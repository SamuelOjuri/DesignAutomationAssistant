from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import (
    DesignProcessingItem,
    DesignProcessingJob,
    MondayWebhookDispatch,
    MondayWebhookEvent,
)
from backend.app import monday_client
from backend.app.monday_client import MondayGroupItem
from backend.app.routes import monday_webhooks
from backend.app.services.design_processing_inputs import (
    DesignProcessingInputError,
    DesignProcessingTargetSnapshot,
)
from backend.app.services.design_processing_queue import (
    next_readiness_check_at,
    queue_design_processing_snapshot,
    readiness_backoff_seconds,
)
from backend.app.services import design_processing_reconciliation
from backend.app.services.design_processing_reconciliation import (
    reconcile_landing_zone_once,
)
from backend.app.services.design_processing_state import needs_analysis


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
BOARD_ID = "1882196103"
LANDING_GROUP_ID = "group_mkpbd6vy"
PIPELINE_VERSION = "pipeline-v1"


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
        engine.dispose()


@pytest.fixture()
def webhook_client(db_session, monkeypatch):
    monkeypatch.setattr(settings, "monday_signing_secret", "webhook-secret")
    monkeypatch.setattr(settings, "monday_webhook_shared_secret", "shared-secret")
    monkeypatch.setattr(settings, "auto_sync_enabled", True)
    monkeypatch.setattr(settings, "auto_sync_board_id", BOARD_ID)
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics")
    monkeypatch.setattr(settings, "auto_sync_excluded_group_ids", LANDING_GROUP_ID)
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")
    monkeypatch.setattr(settings, "auto_sync_debounce_seconds", 90)
    monkeypatch.setattr(settings, "design_processing_mode", "shadow")
    monkeypatch.setattr(settings, "design_processing_board_id", BOARD_ID)
    monkeypatch.setattr(
        settings,
        "design_processing_landing_group_id",
        LANDING_GROUP_ID,
    )
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", [])

    app = FastAPI()
    app.include_router(monday_webhooks.router)
    app.dependency_overrides[monday_webhooks.get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


def _snapshot(
    *,
    input_revision: str | None = "revision-a",
    name: str = "Human enquiry name",
    group_id: str = LANDING_GROUP_ID,
    item_id: str = "2657106977",
) -> DesignProcessingTargetSnapshot:
    return DesignProcessingTargetSnapshot(
        board_id=BOARD_ID,
        item_id=item_id,
        group_id=group_id,
        name=name,
        email_assets=() if input_revision is None else (object(),),
        input_revision=input_revision,
    )


def _monday_item(
    *,
    item_id: str = "2657106977",
    group_id: str = LANDING_GROUP_ID,
    name: str = "Human enquiry name",
) -> dict[str, object]:
    asset = {
        "id": "300",
        "name": "enquiry.msg",
        "file_extension": "msg",
        "file_size": 128,
        "created_at": "2026-08-05T10:00:00Z",
        "url": "https://monday.invalid/private/300",
        "public_url": None,
    }
    return {
        "id": item_id,
        "name": name,
        "account_id": "acct",
        "board": {"id": BOARD_ID, "name": "Design queue"},
        "group": {"id": group_id, "title": "Landing Zone"},
        "updated_at": "2026-08-05T10:00:00Z",
        "assets": [asset],
        "updates": [],
        "column_values": [
            {
                "id": "file_mkpbm883",
                "type": "file",
                "value": json.dumps(
                    {"files": [{"assetId": "300", "name": "enquiry.msg"}]}
                ),
            }
        ],
    }


def _webhook_payload(
    *,
    trigger_uuid: str,
    column_id: str = "file_mkpbm883",
    event_type: str = "change_column_value",
) -> dict[str, object]:
    return {
        "event": {
            "type": event_type,
            "triggerUuid": trigger_uuid,
            "subscriptionId": "subscription-1",
            "boardId": BOARD_ID,
            "pulseId": "2657106977",
            "groupId": LANDING_GROUP_ID,
            "columnId": column_id,
        }
    }


def _stored_item(
    *,
    desired_revision: str | None = "revision-a",
    analyzed_revision: str | None = None,
    published_revision: str | None = None,
    state: str = "scheduled",
) -> DesignProcessingItem:
    return DesignProcessingItem(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id="2657106977",
        latest_desired_input_revision=desired_revision,
        latest_desired_pipeline_version=(
            PIPELINE_VERSION if desired_revision is not None else None
        ),
        latest_analyzed_input_revision=analyzed_revision,
        latest_analyzed_pipeline_version=(
            PIPELINE_VERSION if analyzed_revision is not None else None
        ),
        latest_published_input_revision=published_revision,
        latest_published_pipeline_version=(
            PIPELINE_VERSION if published_revision is not None else None
        ),
        state=state,
        warnings_json=[],
        created_at=NOW,
        updated_at=NOW,
    )


def _assigned_job(
    *,
    status: str,
    input_revision: str = "revision-a",
    execution_kind: str = "analysis",
    stage: str = "extracting",
) -> DesignProcessingJob:
    return DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id="2657106977",
        trigger_type="test",
        execution_kind=execution_kind,
        execution_input_revision=input_revision,
        execution_pipeline_version=PIPELINE_VERSION,
        status=status,
        stage=stage,
        scheduled_for=NOW,
        attempt_count=1,
        readiness_check_count=0,
        max_attempts=3,
        locked_by="worker-1" if status == "running" else None,
        locked_at=NOW if status == "running" else None,
        heartbeat_at=NOW if status == "running" else None,
        created_at=NOW,
        updated_at=NOW,
    )


def _queue(db_session, snapshot, *, mode="shadow", allowlist=()):
    return queue_design_processing_snapshot(
        db_session,
        snapshot,
        trigger_type="webhook_email",
        mode=mode,
        pipeline_version=PIPELINE_VERSION,
        expected_board_id=BOARD_ID,
        expected_group_id=LANDING_GROUP_ID,
        allowlist_item_ids=allowlist,
        now=NOW,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def test_initial_missing_inputs_queue_name_first_readiness(db_session):
    result = _queue(
        db_session,
        _snapshot(input_revision=None, name=""),
    )
    db_session.commit()

    assert result.outcome == "queued"
    assert result.item is not None
    assert result.item.state == "waiting_for_name"
    assert result.job is not None
    assert result.job.stage == "waiting_for_name"
    assert result.job.attempt_count == 0
    assert result.job.readiness_check_count == 0


def test_relevant_webhooks_coalesce_and_wake_readiness_job(db_session):
    first = _queue(db_session, _snapshot(input_revision=None))
    assert first.job is not None
    first.job.status = "retry_wait"
    first.job.scheduled_for = NOW + timedelta(hours=1)
    first.job.next_retry_at = first.job.scheduled_for
    db_session.commit()

    second = _queue(db_session, _snapshot(input_revision="revision-a"))
    db_session.commit()

    assert second.outcome == "coalesced"
    assert second.job is not None
    assert second.job.id == first.job.id
    assert second.job.status == "scheduled"
    assert _as_utc(second.job.scheduled_for) == NOW
    assert second.job.next_retry_at is None
    assert second.job.stage is None
    assert db_session.query(DesignProcessingJob).count() == 1


def test_running_execution_is_immutable_and_records_supersession(db_session):
    item = _stored_item()
    job = _assigned_job(status="running")
    db_session.add_all([item, job])
    db_session.commit()

    result = _queue(db_session, _snapshot(input_revision="revision-b"))
    db_session.commit()

    assert result.outcome == "coalesced"
    assert result.job is job
    assert job.execution_input_revision == "revision-a"
    assert job.status == "running"
    assert job.trigger_type == "test"
    assert item.latest_desired_input_revision == "revision-b"
    assert _as_utc(item.supersession_requested_at) == NOW
    assert db_session.query(DesignProcessingJob).count() == 1


def test_assigned_retry_is_cancelled_before_successor_insert(db_session):
    item = _stored_item()
    stale_job = _assigned_job(status="retry_wait")
    db_session.add_all([item, stale_job])
    db_session.commit()

    result = _queue(db_session, _snapshot(input_revision="revision-b"))
    db_session.commit()

    jobs = db_session.query(DesignProcessingJob).order_by(
        DesignProcessingJob.created_at,
        DesignProcessingJob.id,
    ).all()
    assert result.outcome == "coalesced"
    assert result.job is not None
    assert result.job.id != stale_job.id
    assert stale_job.status == "cancelled"
    assert stale_job.superseded_by_revision == "revision-b"
    assert result.job.status == "scheduled"
    assert result.job.execution_kind is None
    assert len([job for job in jobs if job.status == "scheduled"]) == 1


def test_missing_name_replaces_stale_assigned_retry_with_readiness_job(db_session):
    item = _stored_item()
    stale_job = _assigned_job(status="retry_wait")
    db_session.add_all([item, stale_job])
    db_session.commit()

    result = _queue(db_session, _snapshot(input_revision=None, name=""))
    db_session.commit()

    assert stale_job.status == "cancelled"
    assert item.state == "waiting_for_name"
    assert result.job is not None
    assert result.job.stage == "waiting_for_name"


def test_mode_change_discovers_publication_without_reanalysis(db_session):
    item = _stored_item(
        analyzed_revision="revision-a",
        state="analyzed",
    )
    db_session.add(item)
    db_session.commit()

    shadow = _queue(db_session, _snapshot(), mode="shadow")
    assert shadow.outcome == "ignored"
    assert shadow.job is None

    allowlisted = _queue(
        db_session,
        _snapshot(),
        mode="allowlist",
        allowlist=("2657106977",),
    )
    db_session.commit()

    assert allowlisted.outcome == "queued"
    assert allowlisted.job is not None
    assert allowlisted.job.execution_kind is None
    assert db_session.query(DesignProcessingJob).count() == 1


def test_off_mode_creates_no_state(db_session):
    result = _queue(db_session, _snapshot(), mode="off")
    db_session.commit()

    assert result.outcome == "disabled"
    assert db_session.query(DesignProcessingItem).count() == 0
    assert db_session.query(DesignProcessingJob).count() == 0


def test_readiness_backoff_is_capped_and_does_not_touch_attempts():
    assert readiness_backoff_seconds(
        0,
        initial_interval_seconds=30,
        maximum_interval_seconds=300,
    ) == 30
    assert readiness_backoff_seconds(
        3,
        initial_interval_seconds=30,
        maximum_interval_seconds=300,
    ) == 240
    assert readiness_backoff_seconds(
        4,
        initial_interval_seconds=30,
        maximum_interval_seconds=300,
    ) == 300
    assert next_readiness_check_at(
        NOW,
        4,
        initial_interval_seconds=30,
        maximum_interval_seconds=300,
    ) == NOW + timedelta(seconds=300)


def test_webhook_fetches_once_and_dispatches_consumers_independently(
    webhook_client,
    db_session,
    monkeypatch,
):
    fetches = {"count": 0}

    def fetch_item(token, item_id):
        fetches["count"] += 1
        return _monday_item(item_id=item_id)

    monkeypatch.setattr(
        monday_webhooks,
        "get_monday_ingestion_access_token",
        lambda: "service-token",
    )
    monkeypatch.setattr(
        monday_webhooks,
        "fetch_current_source_revision_inputs",
        fetch_item,
    )

    response = webhook_client.post(
        "/api/monday/webhooks?token=shared-secret",
        json=_webhook_payload(trigger_uuid="dual-dispatch"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert fetches["count"] == 1
    event = db_session.query(MondayWebhookEvent).one()
    dispatches = {
        dispatch.consumer: dispatch
        for dispatch in db_session.query(MondayWebhookDispatch).all()
    }
    assert event.status == "completed"
    assert dispatches["auto_sync"].status == "succeeded"
    assert dispatches["auto_sync"].outcome == "excluded"
    assert dispatches["design_processing"].status == "succeeded"
    assert dispatches["design_processing"].outcome == "queued"
    assert db_session.query(DesignProcessingJob).count() == 1


def test_webhook_retry_processes_only_failed_design_child(
    webhook_client,
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        monday_webhooks,
        "get_monday_ingestion_access_token",
        lambda: "service-token",
    )
    fetches = {"count": 0}

    def fetch_item(token, item_id):
        fetches["count"] += 1
        return _monday_item(item_id=item_id)

    monkeypatch.setattr(
        monday_webhooks,
        "fetch_current_source_revision_inputs",
        fetch_item,
    )
    real_auto_dispatch = monday_webhooks._dispatch_auto_sync
    auto_calls = {"count": 0}

    def count_auto_dispatch(*args, **kwargs):
        auto_calls["count"] += 1
        return real_auto_dispatch(*args, **kwargs)

    monkeypatch.setattr(
        monday_webhooks,
        "_dispatch_auto_sync",
        count_auto_dispatch,
    )
    real_queue = monday_webhooks.queue_design_processing_snapshot
    design_calls = {"count": 0}

    def fail_design_once(*args, **kwargs):
        design_calls["count"] += 1
        if design_calls["count"] == 1:
            raise DesignProcessingInputError("incomplete supported asset metadata")
        return real_queue(*args, **kwargs)

    monkeypatch.setattr(
        monday_webhooks,
        "queue_design_processing_snapshot",
        fail_design_once,
    )
    payload = _webhook_payload(trigger_uuid="retry-design-only")

    first = webhook_client.post(
        "/api/monday/webhooks?token=shared-secret",
        json=payload,
    )
    assert first.status_code == 503
    event = db_session.query(MondayWebhookEvent).one()
    assert event.status == "partial_failed"

    second = webhook_client.post(
        "/api/monday/webhooks?token=shared-secret",
        json=payload,
    )

    assert second.status_code == 200
    db_session.refresh(event)
    assert event.status == "completed"
    assert fetches["count"] == 2
    assert auto_calls["count"] == 1
    assert design_calls["count"] == 2
    dispatches = {
        dispatch.consumer: dispatch
        for dispatch in db_session.query(MondayWebhookDispatch).all()
    }
    assert dispatches["auto_sync"].attempt_count == 1
    assert dispatches["design_processing"].attempt_count == 2


def test_stale_processing_child_is_reclaimed_without_repeating_success(
    webhook_client,
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        monday_webhooks,
        "get_monday_ingestion_access_token",
        lambda: "service-token",
    )
    monkeypatch.setattr(
        monday_webhooks,
        "fetch_current_source_revision_inputs",
        lambda token, item_id: _monday_item(item_id=item_id),
    )
    payload = _webhook_payload(trigger_uuid="stale-child")
    first = webhook_client.post(
        "/api/monday/webhooks?token=shared-secret",
        json=payload,
    )
    assert first.status_code == 200

    event = db_session.query(MondayWebhookEvent).one()
    dispatches = {
        dispatch.consumer: dispatch
        for dispatch in db_session.query(MondayWebhookDispatch).all()
    }
    stale_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    design_dispatch = dispatches["design_processing"]
    design_dispatch.status = "processing"
    design_dispatch.outcome = None
    design_dispatch.processing_started_at = stale_at
    design_dispatch.completed_at = None
    event.status = "processing"
    event.processing_started_at = stale_at
    db_session.commit()

    second = webhook_client.post(
        "/api/monday/webhooks?token=shared-secret",
        json=payload,
    )

    assert second.status_code == 200
    db_session.refresh(event)
    db_session.refresh(dispatches["auto_sync"])
    db_session.refresh(design_dispatch)
    assert event.status == "completed"
    assert dispatches["auto_sync"].attempt_count == 1
    assert design_dispatch.attempt_count == 2
    assert design_dispatch.outcome == "coalesced"


@pytest.mark.parametrize(
    ("event_type", "column_id", "expected_trigger"),
    [
        ("create_item", "", "webhook_create"),
        ("move_pulse_into_group", "", "webhook_move"),
        ("change_column_value", "file_mkpbm883", "webhook_email"),
        ("change_column_value", "name", "webhook_name"),
    ],
)
def test_design_webhook_event_classification(
    event_type,
    column_id,
    expected_trigger,
):
    normalized = monday_webhooks.normalize_webhook_payload(
        _webhook_payload(
            trigger_uuid=f"classify-{expected_trigger}",
            event_type=event_type,
            column_id=column_id,
        )
    )

    assert monday_webhooks._design_trigger_type(normalized) == expected_trigger


@pytest.mark.parametrize("column_id", ["file_mkza7y37", "file_mm59rntf"])
def test_design_dispatch_ignores_worker_output_columns(
    webhook_client,
    db_session,
    monkeypatch,
    column_id,
):
    monkeypatch.setattr(
        monday_webhooks,
        "get_monday_ingestion_access_token",
        lambda: "service-token",
    )
    monkeypatch.setattr(
        monday_webhooks,
        "fetch_current_source_revision_inputs",
        lambda token, item_id: _monday_item(item_id=item_id),
    )

    response = webhook_client.post(
        "/api/monday/webhooks?token=shared-secret",
        json=_webhook_payload(
            trigger_uuid=f"ignored-{column_id}",
            column_id=column_id,
        ),
    )

    assert response.status_code == 200
    dispatch = db_session.query(MondayWebhookDispatch).filter_by(
        consumer="design_processing"
    ).one()
    assert dispatch.status == "succeeded"
    assert dispatch.outcome == "ignored"
    assert db_session.query(DesignProcessingJob).count() == 0


def test_parent_is_failed_when_both_consumers_fail(
    webhook_client,
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        monday_webhooks,
        "get_monday_ingestion_access_token",
        lambda: "service-token",
    )
    monkeypatch.setattr(
        monday_webhooks,
        "fetch_current_source_revision_inputs",
        lambda token, item_id: (_ for _ in ()).throw(
            monday_webhooks.TransientMondayAPIError(detail="temporary outage")
        ),
    )

    response = webhook_client.post(
        "/api/monday/webhooks?token=shared-secret",
        json=_webhook_payload(trigger_uuid="both-fail"),
    )

    assert response.status_code == 503
    event = db_session.query(MondayWebhookEvent).one()
    dispatches = db_session.query(MondayWebhookDispatch).all()
    assert event.status == "failed"
    assert {dispatch.status for dispatch in dispatches} == {"failed"}


class FakeReconciliationGateway:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.calls = []

    def fetch_target(self, item_id):
        self.calls.append(item_id)
        return self.snapshots[item_id]

    def inspect_file_columns(self, item_id):
        raise AssertionError("not used")

    def fetch_project_word_hit_counts(self, words):
        raise AssertionError("not used")

    def fetch_project_items_matching_words(self, words, *, start_date="2021-01-01"):
        raise AssertionError("not used")

    def fetch_project_items_matching_full_text(
        self,
        project_name,
        *,
        start_date="2021-01-01",
    ):
        raise AssertionError("not used")

    def fetch_active_project_items_since(self, *, start_date="2021-01-01"):
        raise AssertionError("not used")


def test_reconciliation_requires_activation_boundary_for_broad_scan(db_session):
    with pytest.raises(ValueError, match="activation timestamp"):
        reconcile_landing_zone_once(
            db_session,
            access_token="token",
            gateway=FakeReconciliationGateway({}),
            mode="shadow",
        )


def test_group_listing_preserves_created_at_for_activation_filter(monkeypatch):
    queries = []

    def request(access_token, query, variables=None, *, timeout=10, **kwargs):
        queries.append(query)
        return {
            "data": {
                "boards": [
                    {
                        "groups": [
                            {
                                "id": LANDING_GROUP_ID,
                                "items_page": {
                                    "cursor": None,
                                    "items": [
                                        {
                                            "id": "2657106977",
                                            "created_at": "2026-08-05T10:00:00Z",
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ]
            }
        }

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)

    summaries = monday_client.list_items_in_groups(
        "token",
        BOARD_ID,
        [LANDING_GROUP_ID],
    )
    identifiers = monday_client.list_item_ids_in_groups(
        "token",
        BOARD_ID,
        [LANDING_GROUP_ID],
    )

    assert summaries[LANDING_GROUP_ID] == [
        MondayGroupItem("2657106977", "2026-08-05T10:00:00Z")
    ]
    assert identifiers[LANDING_GROUP_ID] == ["2657106977"]
    assert all("created_at" in query for query in queries)


def test_reconciliation_is_activation_bounded_and_idempotent(
    db_session,
    monkeypatch,
):
    boundary = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    gateway = FakeReconciliationGateway(
        {"2": _snapshot(item_id="2", input_revision="revision-b")}
    )
    monkeypatch.setattr(
        design_processing_reconciliation,
        "list_items_in_groups",
        lambda *args, **kwargs: {
            LANDING_GROUP_ID: [
                MondayGroupItem("1", "2026-08-05T09:59:59Z"),
                MondayGroupItem("2", "2026-08-05T10:00:00Z"),
            ]
        },
    )

    first = reconcile_landing_zone_once(
        db_session,
        dry_run=False,
        access_token="token",
        gateway=gateway,
        mode="shadow",
        activation_timestamp=boundary,
        now=NOW,
    )
    second = reconcile_landing_zone_once(
        db_session,
        dry_run=False,
        access_token="token",
        gateway=gateway,
        mode="shadow",
        activation_timestamp=boundary,
        now=NOW,
    )

    assert first.scanned == 1
    assert first.queued == 1
    assert first.skipped == 1
    assert first.items[0].reason == "before_activation_timestamp"
    assert second.scanned == 1
    assert second.coalesced == 1
    assert db_session.query(DesignProcessingJob).count() == 1
    assert gateway.calls == ["2", "2"]


def test_item_scoped_reconciliation_dry_run_has_no_database_effects(db_session):
    gateway = FakeReconciliationGateway(
        {"9": _snapshot(item_id="9", input_revision="revision-z")}
    )

    result = reconcile_landing_zone_once(
        db_session,
        dry_run=True,
        access_token="token",
        gateway=gateway,
        mode="shadow",
        item_id="9",
        now=NOW,
    )

    assert result.scanned == 1
    assert result.queued == 1
    assert result.items[0].action == "would_queued"
    assert db_session.query(DesignProcessingItem).count() == 0
    assert db_session.query(DesignProcessingJob).count() == 0


def test_reconciliation_discovers_publication_only_after_allowlist_change(
    db_session,
    monkeypatch,
):
    pipeline_version = settings.design_processing_pipeline_version
    item = DesignProcessingItem(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id="2657106977",
        latest_desired_input_revision="revision-a",
        latest_desired_pipeline_version=pipeline_version,
        latest_analyzed_input_revision="revision-a",
        latest_analyzed_pipeline_version=pipeline_version,
        state="analyzed",
        warnings_json=[],
        created_at=NOW,
        updated_at=NOW,
    )
    db_session.add(item)
    db_session.commit()
    monkeypatch.setattr(
        settings,
        "design_processing_allowlist_item_ids",
        ["2657106977"],
    )
    gateway = FakeReconciliationGateway(
        {"2657106977": _snapshot(input_revision="revision-a")}
    )

    result = reconcile_landing_zone_once(
        db_session,
        dry_run=False,
        access_token="token",
        gateway=gateway,
        mode="allowlist",
        item_id="2657106977",
        now=NOW,
    )

    db_session.refresh(item)
    job = db_session.query(DesignProcessingJob).one()
    assert result.queued == 1
    assert needs_analysis(item) is False
    assert job.execution_kind is None
    assert item.state == "analyzed"
