from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.auth import CurrentUser
from backend.app.db import Base
from backend.app.models import AutoSyncJob, HandoffCode, Task, TaskSnapshot, UserMondayLink
from backend.app.routes import monday_handoff
from backend.app.schemas import HandoffResolveRequest
from backend.app.services.auto_sync_policy import AutoSyncPolicy
from backend.app.services.auto_sync_reconciliation import (
    detect_completed_transitions_once,
    reconcile_active_items_once,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()


class FakeBackgroundTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, *args):
        self.calls.append(args)


def _policy() -> AutoSyncPolicy:
    return AutoSyncPolicy(
        enabled=True,
        board_id="1882196103",
        active_group_ids=frozenset({"topics"}),
        excluded_group_ids=frozenset({"group_mkpbd6vy"}),
        completed_group_id="group_mkpbb3tx",
        retention_days=30,
        debounce_seconds=90,
        backfill_batch_size=10,
    )


def _task(item_id: str, *, sync_status: str = "completed", revision: str | None = None) -> Task:
    return Task(
        external_task_key=f"acct:1882196103:{item_id}",
        account_id="acct",
        board_id="1882196103",
        item_id=item_id,
        auto_sync_enabled=True,
        auto_sync_state="active",
        sync_status=sync_status,
        latest_snapshot_version=revision,
        last_indexed_source_revision=revision,
    )


def _handoff_fixture(db_session, *, task: Task, snapshot_revision: str | None = None) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    db_session.add(
        HandoffCode(
            code="handoff-code",
            monday_account_id="acct",
            monday_board_id="1882196103",
            monday_item_id=task.item_id,
            monday_user_id="monday-user",
            expires_at=expires_at,
            used=False,
        )
    )
    db_session.add(
        UserMondayLink(
            id=uuid.uuid4(),
            target_user_id="app-user",
            monday_user_id="monday-user",
            monday_account_id="acct",
            access_token="user-token",
        )
    )
    db_session.add(task)
    if snapshot_revision:
        db_session.add(
            TaskSnapshot(
                id=uuid.uuid4(),
                external_task_key=task.external_task_key,
                snapshot_version=snapshot_revision,
                task_context_json={"id": task.item_id},
            )
        )
    db_session.commit()


def test_handoff_resolve_skips_sync_for_fresh_completed_snapshot(db_session, monkeypatch):
    task = _task("item-1", revision="rev-1")
    _handoff_fixture(db_session, task=task, snapshot_revision="rev-1")
    background_tasks = FakeBackgroundTasks()

    monkeypatch.setattr(monday_handoff, "can_read_item", lambda access_token, item_id: True)
    monkeypatch.setattr(
        monday_handoff,
        "fetch_desired_source_revision",
        lambda item_id, access_token=None: "rev-1",
    )

    response = monday_handoff.handoff_resolve(
        HandoffResolveRequest(code="handoff-code"),
        db=db_session,
        current_user=CurrentUser(id="app-user"),
        background_tasks=background_tasks,
    )

    db_session.refresh(task)
    assert response.externalTaskKey == task.external_task_key
    assert background_tasks.calls == []
    assert task.sync_status == "completed"


def test_handoff_resolve_falls_back_to_sync_for_stale_snapshot(db_session, monkeypatch):
    task = _task("item-1", revision="rev-1")
    _handoff_fixture(db_session, task=task, snapshot_revision="rev-1")
    background_tasks = FakeBackgroundTasks()

    monkeypatch.setattr(monday_handoff, "can_read_item", lambda access_token, item_id: True)
    monkeypatch.setattr(
        monday_handoff,
        "fetch_desired_source_revision",
        lambda item_id, access_token=None: "rev-2",
    )

    monday_handoff.handoff_resolve(
        HandoffResolveRequest(code="handoff-code"),
        db=db_session,
        current_user=CurrentUser(id="app-user"),
        background_tasks=background_tasks,
    )

    db_session.refresh(task)
    assert task.sync_status == "syncing"
    assert task.sync_completed_at is None
    assert len(background_tasks.calls) == 1
    assert background_tasks.calls[0][1:] == (task.external_task_key, "user-token", False)


def test_active_reconciliation_queues_missing_stale_failed_and_stuck_items(db_session, monkeypatch):
    now = datetime.now(timezone.utc)
    fresh = _task("fresh", revision="rev-fresh")
    stale = _task("stale", revision="old-rev")
    failed = _task("failed", sync_status="failed", revision="rev-failed")
    stuck = _task("stuck", sync_status="syncing", revision="rev-stuck")
    stuck.sync_started_at = now - timedelta(hours=2)
    db_session.add_all([fresh, stale, failed, stuck])
    db_session.commit()

    revisions = {
        "fresh": "rev-fresh",
        "stale": "rev-stale",
        "failed": "rev-failed",
        "stuck": "rev-stuck",
        "missing": "rev-missing",
    }

    monkeypatch.setattr(
        "backend.app.services.auto_sync_reconciliation.fetch_current_account_id",
        lambda token: "acct",
    )
    monkeypatch.setattr(
        "backend.app.services.auto_sync_reconciliation.list_item_ids_in_groups",
        lambda token, board_id, group_ids, limit: {"topics": list(revisions.keys())},
    )
    monkeypatch.setattr(
        "backend.app.services.auto_sync_reconciliation.fetch_current_source_revision_inputs",
        lambda token, item_id, *, account_id=None: {
            "id": item_id,
            "account_id": account_id,
            "board": {"id": "1882196103"},
            "group": {"id": "topics", "title": "Hub A - Outstanding"},
            "updated_at": "2026-07-01T12:00:00Z",
            "assets": [],
            "updates": [],
        },
    )
    monkeypatch.setattr(
        "backend.app.services.auto_sync_reconciliation.compute_desired_source_revision",
        lambda item: revisions[item["id"]],
    )

    result = reconcile_active_items_once(
        db_session,
        dry_run=False,
        access_token="service-token",
        policy=_policy(),
        stuck_after_seconds=3600,
    )

    jobs = db_session.query(AutoSyncJob).order_by(AutoSyncJob.item_id.asc()).all()
    actions = {item.item_id: item.action for item in result.items}
    desired_by_item = {job.item_id: job.desired_source_revision for job in jobs}

    assert result.queued == 4
    assert result.skipped == 1
    assert actions["fresh"] == "fresh"
    assert actions["stale"] == "queued_stale"
    assert actions["failed"] == "queued_failed"
    assert actions["stuck"] == "queued_stuck"
    assert actions["missing"] == "queued_missing"
    assert set(desired_by_item) == {"failed", "missing", "stale", "stuck"}
    assert desired_by_item["failed"] is None
    assert desired_by_item["stuck"] is None
    assert desired_by_item["stale"] == "rev-stale"
    assert desired_by_item["missing"] == "rev-missing"


def test_completed_transition_detection_marks_indexed_active_task_retained(db_session, monkeypatch):
    task = _task("done", revision="rev-done")
    db_session.add(task)
    db_session.commit()

    monkeypatch.setattr(
        "backend.app.services.auto_sync_reconciliation.fetch_item_metadata",
        lambda token, item_id: {
            "id": item_id,
            "account_id": "acct",
            "board": {"id": "1882196103"},
            "group": {"id": "group_mkpbb3tx", "title": "Completed Folder"},
        },
    )

    result = detect_completed_transitions_once(
        db_session,
        dry_run=False,
        access_token="service-token",
        policy=_policy(),
    )

    db_session.refresh(task)
    assert result.scanned == 1
    assert result.completed_retained == 1
    assert result.items[0].action == "completed_retained"
    assert task.auto_sync_state == "completed_retained"
    assert task.completed_at is not None
    assert task.purge_after is not None
