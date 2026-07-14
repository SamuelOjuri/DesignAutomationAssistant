from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import AutoSyncJob, MondayWebhookEvent, Task
from backend.app.routes import monday_webhooks
from backend.app.services import auto_sync as auto_sync_service


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def auto_sync_settings(monkeypatch):
    monkeypatch.setattr(settings, "monday_signing_secret", "webhook-secret")
    monkeypatch.setattr(settings, "monday_webhook_shared_secret", None)
    monkeypatch.setattr(settings, "backend_base_url", "https://design-automation-assistant-api.onrender.com")
    monkeypatch.setattr(settings, "auto_sync_enabled", True)
    monkeypatch.setattr(settings, "auto_sync_board_id", "1882196103")
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics,group_mkpbs35c,group_mkqbx92r")
    monkeypatch.setattr(settings, "auto_sync_excluded_group_ids", "group_mkpbd6vy")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "group_mkpbb3tx")
    monkeypatch.setattr(settings, "auto_sync_retention_days", 30)
    monkeypatch.setattr(settings, "auto_sync_debounce_seconds", 90)


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(monday_webhooks.router)
    app.dependency_overrides[monday_webhooks.get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


def _auth_headers(*, audience: str | None = None, expires_delta: timedelta = timedelta(minutes=5)) -> dict[str, str]:
    claims = {
        "iss": "monday.com",
        "exp": datetime.now(timezone.utc) + expires_delta,
    }
    if audience is not None:
        claims["aud"] = audience
    token = jwt.encode(claims, settings.monday_signing_secret, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _webhook_payload(*, trigger_uuid: str = "trigger-1", item_id: str = "item-1", group_id: str = "topics") -> dict:
    return {
        "event": {
            "type": "change_column_value",
            "triggerUuid": trigger_uuid,
            "subscriptionId": "subscription-1",
            "boardId": "1882196103",
            "pulseId": item_id,
            "groupId": group_id,
            "columnId": "files",
        }
    }


def _monday_item(*, item_id: str = "item-1", group_id: str = "topics", group_title: str = "Hub A") -> dict:
    return {
        "id": item_id,
        "account_id": "acct",
        "board": {"id": "1882196103", "name": "Design queue"},
        "group": {"id": group_id, "title": group_title},
        "updated_at": "2026-07-01T12:00:00Z",
        "assets": [],
        "updates": [],
        "column_values": [],
    }


class FakeDeadlockError(Exception):
    pgcode = "40P01"


def test_webhook_challenge_responds_without_auth(client):
    response = client.post("/api/monday/webhooks", json={"challenge": "challenge-token"})

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-token"}


def test_webhook_requires_valid_authorization_before_persisting(client, db_session):
    response = client.post(
        "/api/monday/webhooks",
        json=_webhook_payload(),
        headers={"Authorization": "Bearer not-a-valid-token"},
    )

    assert response.status_code == 401
    assert db_session.query(MondayWebhookEvent).count() == 0


def test_webhook_accepts_shared_secret_query_token(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "monday_webhook_shared_secret", "shared-secret")
    payload = _webhook_payload(trigger_uuid="shared-secret-event")
    payload["event"]["boardId"] = "other-board"

    response = client.post("/api/monday/webhooks?token=shared-secret", json=payload)

    event = db_session.query(MondayWebhookEvent).one()
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert event.authenticated is True
    assert event.board_id == "other-board"
    assert event.status == "ignored"
    assert event.error == "board_not_managed"


def test_webhook_rejects_invalid_shared_secret_before_persisting(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "monday_webhook_shared_secret", "shared-secret")

    response = client.post("/api/monday/webhooks?token=wrong-secret", json=_webhook_payload())

    assert response.status_code == 401
    assert db_session.query(MondayWebhookEvent).count() == 0


def test_auto_sync_task_upsert_recovers_from_concurrent_insert(db_session, monkeypatch):
    metadata = auto_sync_service.ItemMetadata(
        account_id="acct",
        board_id="1882196103",
        item_id="race-item",
        group_id="group_mkpbd6vy",
        group_title="Landing Zone",
        external_task_key="acct:1882196103:race-item",
    )
    db_session.add(
        Task(
            external_task_key=metadata.external_task_key,
            account_id=metadata.account_id,
            board_id=metadata.board_id,
            item_id=metadata.item_id,
            auto_sync_enabled=True,
            auto_sync_state="active",
            source_group_id="topics",
        )
    )
    db_session.commit()
    db_session.expunge_all()

    real_get = db_session.get
    stale_gets = {"remaining": 1}

    def stale_task_get(model, ident, *args, **kwargs):
        if model is Task and ident == metadata.external_task_key and stale_gets["remaining"]:
            stale_gets["remaining"] -= 1
            return None
        return real_get(model, ident, *args, **kwargs)

    monkeypatch.setattr(db_session, "get", stale_task_get)

    task, created = auto_sync_service.upsert_auto_sync_task(
        db_session,
        metadata,
        lifecycle_state="excluded",
        now=datetime(2026, 7, 6, 14, 33, tzinfo=timezone.utc),
    )
    db_session.commit()

    assert created is False
    assert task.external_task_key == metadata.external_task_key
    assert task.auto_sync_state == "excluded"
    assert task.source_group_id == "group_mkpbd6vy"
    assert task.source_group_title == "Landing Zone"
    assert db_session.query(Task).filter_by(external_task_key=metadata.external_task_key).count() == 1


def test_webhook_persists_event_and_coalesces_jobs(client, db_session, monkeypatch):
    revisions = {"value": "rev-1"}

    monkeypatch.setattr(monday_webhooks, "get_monday_ingestion_access_token", lambda: "service-token")
    monkeypatch.setattr(
        monday_webhooks,
        "fetch_current_source_revision_inputs",
        lambda token, item_id: _monday_item(item_id=item_id),
    )
    monkeypatch.setattr(monday_webhooks, "compute_desired_source_revision", lambda item: revisions["value"])

    headers = _auth_headers(audience="https://design-automation-assistant-api.onrender.com/api/monday/webhooks")
    first_response = client.post("/api/monday/webhooks", json=_webhook_payload(), headers=headers)

    assert first_response.status_code == 200
    assert first_response.json()["status"] == "queued"
    assert first_response.json()["decision"] == "active_group"

    task = db_session.get(Task, "acct:1882196103:item-1")
    jobs = db_session.query(AutoSyncJob).all()
    events = db_session.query(MondayWebhookEvent).all()

    assert task is not None
    assert task.sync_status == "queued"
    assert task.last_sync_trigger == "webhook"
    assert len(jobs) == 1
    assert jobs[0].status == "scheduled"
    assert jobs[0].desired_source_revision == "rev-1"
    assert len(events) == 1
    assert events[0].authenticated is True
    assert events[0].status == "queued"

    duplicate_response = client.post("/api/monday/webhooks", json=_webhook_payload(), headers=headers)
    assert duplicate_response.status_code == 200
    assert duplicate_response.json()["status"] == "duplicate"
    assert db_session.query(MondayWebhookEvent).count() == 1
    assert db_session.query(AutoSyncJob).count() == 1

    revisions["value"] = "rev-2"
    second_payload = _webhook_payload(trigger_uuid="trigger-2")
    second_response = client.post("/api/monday/webhooks", json=second_payload, headers=headers)

    job = db_session.query(AutoSyncJob).one()
    assert second_response.status_code == 200
    assert second_response.json()["status"] == "queued"
    assert db_session.query(MondayWebhookEvent).count() == 2
    assert job.desired_source_revision == "rev-2"


def test_webhook_uses_current_item_state_for_out_of_order_events(client, db_session, monkeypatch):
    task = Task(
        external_task_key="acct:1882196103:item-2",
        account_id="acct",
        board_id="1882196103",
        item_id="item-2",
        auto_sync_enabled=True,
        auto_sync_state="active",
        sync_status="completed",
        last_indexed_source_revision="rev-current",
    )
    db_session.add(task)
    db_session.commit()

    monkeypatch.setattr(monday_webhooks, "get_monday_ingestion_access_token", lambda: "service-token")
    monkeypatch.setattr(
        monday_webhooks,
        "fetch_current_source_revision_inputs",
        lambda token, item_id: _monday_item(
            item_id=item_id,
            group_id="group_mkpbb3tx",
            group_title="Completed Folder",
        ),
    )
    monkeypatch.setattr(monday_webhooks, "compute_desired_source_revision", lambda item: "rev-current")

    response = client.post(
        "/api/monday/webhooks",
        json=_webhook_payload(trigger_uuid="late-active-event", item_id="item-2", group_id="topics"),
        headers=_auth_headers(),
    )

    db_session.refresh(task)
    event = db_session.query(MondayWebhookEvent).one()

    assert response.status_code == 200
    assert response.json()["status"] == "retained"
    assert event.status == "retained"
    assert db_session.query(AutoSyncJob).count() == 0
    assert task.auto_sync_state == "completed_retained"
    assert task.completed_at is not None
    assert task.purge_after is not None


def test_webhook_retries_deadlock_and_commits_once(client, db_session, monkeypatch):
    monkeypatch.setattr(monday_webhooks, "get_monday_ingestion_access_token", lambda: "service-token")
    monkeypatch.setattr(
        monday_webhooks,
        "fetch_current_source_revision_inputs",
        lambda token, item_id: _monday_item(item_id=item_id),
    )
    monkeypatch.setattr(monday_webhooks, "compute_desired_source_revision", lambda item: "rev-deadlock")
    monkeypatch.setattr("backend.app.services.db_retry.time.sleep", lambda delay: None)

    real_apply = monday_webhooks.apply_auto_sync_policy_for_item
    calls = {"count": 0}

    def flaky_apply(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OperationalError("UPDATE auto_sync_jobs", {}, FakeDeadlockError())
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(monday_webhooks, "apply_auto_sync_policy_for_item", flaky_apply)

    response = client.post(
        "/api/monday/webhooks",
        json=_webhook_payload(trigger_uuid="deadlock-then-success"),
        headers=_auth_headers(),
    )

    event = db_session.query(MondayWebhookEvent).one()
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert calls["count"] == 2
    assert event.status == "queued"
    assert event.attempt_count == 1
    assert db_session.query(AutoSyncJob).count() == 1


def test_failed_deadlock_event_can_be_redelivered(client, db_session, monkeypatch):
    monkeypatch.setattr(monday_webhooks, "get_monday_ingestion_access_token", lambda: "service-token")
    monkeypatch.setattr(
        monday_webhooks,
        "fetch_current_source_revision_inputs",
        lambda token, item_id: _monday_item(item_id=item_id),
    )
    monkeypatch.setattr(monday_webhooks, "compute_desired_source_revision", lambda item: "rev-recovered")
    monkeypatch.setattr("backend.app.services.db_retry.time.sleep", lambda delay: None)

    real_apply = monday_webhooks.apply_auto_sync_policy_for_item
    monkeypatch.setattr(
        monday_webhooks,
        "apply_auto_sync_policy_for_item",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OperationalError("UPDATE auto_sync_jobs", {}, FakeDeadlockError())
        ),
    )
    payload = _webhook_payload(trigger_uuid="deadlock-redelivery")
    first_response = client.post("/api/monday/webhooks", json=payload, headers=_auth_headers())

    event = db_session.query(MondayWebhookEvent).one()
    assert first_response.status_code == 503
    assert first_response.headers["retry-after"] == "1"
    assert event.status == "failed"
    assert event.attempt_count == 1

    monkeypatch.setattr(monday_webhooks, "apply_auto_sync_policy_for_item", real_apply)
    second_response = client.post("/api/monday/webhooks", json=payload, headers=_auth_headers())

    db_session.refresh(event)
    assert second_response.status_code == 200
    assert second_response.json()["status"] == "queued"
    assert event.status == "queued"
    assert event.attempt_count == 2
    assert db_session.query(MondayWebhookEvent).count() == 1
    assert db_session.query(AutoSyncJob).count() == 1