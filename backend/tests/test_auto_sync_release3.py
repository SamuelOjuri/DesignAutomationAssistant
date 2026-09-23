from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import logging
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.auth import CurrentUser
from backend.app import monday_client
from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import AppUser, AutoSyncJob, AutoSyncReconciliationCheck, HandoffCode, Task, TaskSnapshot, UserMondayLink
from backend.app.routes import monday_handoff, tasks
from backend.app.schemas import HandoffResolveRequest, TaskSyncRequest
from backend.app.services import auto_sync_reconciliation, auto_sync_worker
from backend.app.services.auto_sync import coalesce_auto_sync_job
from backend.app.services.db_retry import AutoSyncConcurrencyError
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


@pytest.fixture(autouse=True)
def durable_sync_settings(monkeypatch):
    monkeypatch.setattr(settings, "auto_sync_board_id", "1882196103")
    monkeypatch.setattr(settings, "auto_sync_worker_enabled", True)
    monkeypatch.setattr(settings, "monday_ingestion_access_token", "service-token")


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


def test_reconciliation_cli_reports_http_exception(monkeypatch, capsys):
    def fail_from_new_session(args):
        raise HTTPException(status_code=502, detail="monday API error (502)")

    monkeypatch.setattr(auto_sync_reconciliation, "_run_from_new_session", fail_from_new_session)
    monkeypatch.setattr("sys.argv", ["auto_sync_reconciliation", "--limit", "25"])

    assert auto_sync_reconciliation.main() == 1

    captured = capsys.readouterr()
    assert "Auto-sync reconciliation failed: monday API error (502)" in captured.out


@pytest.mark.parametrize("scope", ["active", "completed"])
def test_reconciliation_cli_fails_when_any_item_failed(monkeypatch, scope):
    monkeypatch.setattr("sys.argv", ["auto_sync_reconciliation"])
    monkeypatch.setattr(
        auto_sync_reconciliation, "_run_from_new_session",
        lambda args: (
            auto_sync_reconciliation.ReconciliationResult(dry_run=False, board_id="board", errors=int(scope == "active")),
            auto_sync_reconciliation.ReconciliationResult(dry_run=False, board_id="board", errors=int(scope == "completed")),
        ),
    )
    assert auto_sync_reconciliation.main() == 1


def _handoff_fixture(db_session, *, task: Task, snapshot_revision: str | None = None) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    db_session.add(
        HandoffCode(
            code="handoff-code",
            monday_account_id="acct",
            monday_board_id=task.board_id,
            monday_item_id=task.item_id,
            monday_user_id="monday-user",
            expires_at=expires_at,
            used=False,
        )
    )
    db_session.add(
        AppUser(
            id="app-user",
            monday_account_id="acct",
            monday_user_id="monday-user",
        )
    )
    db_session.add(
        UserMondayLink(
            id=uuid.uuid4(),
            target_user_id="app-user",
            app_user_id="app-user",
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


@pytest.mark.parametrize("sync_status", ["completed", "syncing", "queued"])
def test_handoff_resolve_skips_sync_for_fresh_completed_snapshot(db_session, monkeypatch, caplog, sync_status):
    caplog.set_level(logging.INFO, logger="backend.app.services.auto_sync")
    task = _task("item-1", revision="rev-1", sync_status=sync_status)
    _handoff_fixture(db_session, task=task, snapshot_revision="rev-1")
    background_tasks = FakeBackgroundTasks()
    monkeypatch.setattr(settings, "auto_sync_worker_enabled", False)

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
    assert task.sync_status == sync_status
    assert db_session.query(AutoSyncJob).count() == 0
    assert db_session.get(HandoffCode, "handoff-code").used is True
    decision = next(record for record in caplog.records if getattr(record, "event", None) == "auto_sync.refresh_decision")
    assert decision.refresh_action == "skipped"
    assert decision.refresh_reason == "fresh"


def test_handoff_resolve_enqueues_durable_sync_for_stale_snapshot(db_session, monkeypatch):
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
    assert task.sync_status == "queued"
    assert task.sync_completed_at is None
    assert background_tasks.calls == []
    job = db_session.query(AutoSyncJob).one()
    assert job.trigger_type == "handoff"
    assert job.desired_source_revision == "rev-2"
    assert task.latest_snapshot_version == "rev-1"


@pytest.mark.parametrize("scenario", ["new_task", "missing_snapshot", "expired", "force", "lookup_failed"])
def test_managed_handoff_persists_refresh_intent(db_session, monkeypatch, caplog, scenario):
    caplog.set_level(logging.INFO, logger="backend.app.services.auto_sync")
    task = _task("item-1", revision="rev-1")
    snapshot_revision = "rev-1" if scenario in {"force", "lookup_failed"} else None
    _handoff_fixture(db_session, task=task, snapshot_revision=snapshot_revision)
    task_key = task.external_task_key
    if scenario == "new_task":
        db_session.delete(task)
    elif scenario == "expired":
        task.auto_sync_state = "expired"
    db_session.commit()
    monkeypatch.setattr(monday_handoff, "can_read_item", lambda *args: True)

    def source_revision(*args, **kwargs):
        if scenario == "lookup_failed":
            raise RuntimeError("Monday unavailable")
        return "rev-1"

    monkeypatch.setattr(monday_handoff, "fetch_desired_source_revision", source_revision)
    background = FakeBackgroundTasks()
    response = monday_handoff.handoff_resolve(
        HandoffResolveRequest(code="handoff-code", force=scenario == "force"),
        db=db_session, current_user=CurrentUser(id="app-user"), background_tasks=background,
    )
    assert response.externalTaskKey == task_key
    assert background.calls == []
    decision = next(record for record in caplog.records if getattr(record, "event", None) == "auto_sync.refresh_decision")
    assert decision.refresh_reason == {
        "new_task": "missing", "missing_snapshot": "missing_snapshot", "expired": "restore",
        "force": "force", "lookup_failed": "freshness_unavailable",
    }[scenario]
    assert decision.sync_trigger == "handoff"
    assert decision.desired_generation == 1
    assert "user-token" not in caplog.text
    assert "service-token" not in caplog.text
    with sessionmaker(bind=db_session.bind, autoflush=False)() as worker_db:
        job = worker_db.query(AutoSyncJob).one()
        assert job.force_requested == (scenario == "force")
        assert job.desired_source_revision == (None if scenario == "lookup_failed" else "rev-1")
        assert job.trigger_type == "handoff"
        assert worker_db.get(HandoffCode, "handoff-code").used is True
        if snapshot_revision:
            assert worker_db.query(TaskSnapshot).filter_by(ingestion_status="complete").count() == 1
        calls = []

        def pipeline(db, external_task_key, access_token, force):
            calls.append((external_task_key, access_token, force))
            return SimpleNamespace(status="done", snapshot_version="rev-2")

        result = auto_sync_worker.run_due_jobs_once(worker_db, pipeline_runner=pipeline)
        assert result.completed == 1
        assert calls == [(task_key, "service-token", scenario == "force")]


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("job_status", ["scheduled", "running"])
def test_manual_refresh_coalesces_even_when_auto_sync_excluded(db_session, monkeypatch, force, job_status):
    task = _task("item-1", revision="rev-1")
    task.auto_sync_enabled = False
    task.auto_sync_state = "excluded"
    _handoff_fixture(db_session, task=task, snapshot_revision="rev-1")
    job, _ = coalesce_auto_sync_job(
        db_session, task, trigger_type="webhook", desired_source_revision="rev-1",
        scheduled_for=datetime.now(timezone.utc),
    )
    db_session.commit()
    if job_status == "running":
        auto_sync_worker.claim_due_jobs(db_session, worker_id="worker")
    monkeypatch.setattr(tasks, "can_read_item", lambda *args: True)
    background = FakeBackgroundTasks()
    response = tasks.sync_task(
        task.external_task_key, TaskSyncRequest(force=force), db=db_session,
        current_user=CurrentUser(id="app-user"), background_tasks=background,
    )
    assert response.status == "queued"
    assert response.snapshotVersion == "rev-1"
    assert db_session.query(AutoSyncJob).count() == 1
    assert job.status == job_status
    assert job.desired_generation == 2
    assert job.force_requested == force
    assert job.desired_source_revision is None
    assert task.auto_sync_enabled is False
    assert task.auto_sync_state == "excluded"
    assert task.last_meaningful_access_at is not None
    assert background.calls == []


@pytest.mark.parametrize("missing_setting", ["auto_sync_worker_enabled", "monday_ingestion_access_token"])
def test_unavailable_worker_does_not_consume_handoff(db_session, monkeypatch, missing_setting):
    task = _task("item-1", revision="rev-1")
    _handoff_fixture(db_session, task=task, snapshot_revision="rev-1")
    monkeypatch.setattr(settings, missing_setting, False if missing_setting.endswith("enabled") else None)
    monkeypatch.setattr(monday_handoff, "can_read_item", lambda *args: True)
    monkeypatch.setattr(monday_handoff, "fetch_desired_source_revision", lambda *args, **kwargs: "rev-2")
    with pytest.raises(HTTPException) as caught:
        monday_handoff.handoff_resolve(
            HandoffResolveRequest(code="handoff-code"), db=db_session,
            current_user=CurrentUser(id="app-user"), background_tasks=FakeBackgroundTasks(),
        )
    assert caught.value.status_code == 503
    assert db_session.get(HandoffCode, "handoff-code").used is False
    assert task.sync_status == "completed"
    assert db_session.query(AutoSyncJob).count() == 0


def test_handoff_enqueue_retries_transaction_atomically(db_session, monkeypatch):
    task = _task("item-1", revision="rev-1")
    _handoff_fixture(db_session, task=task)
    monkeypatch.setattr(monday_handoff, "can_read_item", lambda *args: True)
    monkeypatch.setattr(monday_handoff, "fetch_desired_source_revision", lambda *args, **kwargs: "rev-2")
    real_enqueue = monday_handoff.enqueue_user_refresh
    attempts = []

    def conflicted_enqueue(*args, **kwargs):
        result = real_enqueue(*args, **kwargs)
        attempts.append(1)
        if len(attempts) == 1:
            raise AutoSyncConcurrencyError("concurrent insert")
        return result

    monkeypatch.setattr(monday_handoff, "enqueue_user_refresh", conflicted_enqueue)
    monday_handoff.handoff_resolve(
        HandoffResolveRequest(code="handoff-code"), db=db_session,
        current_user=CurrentUser(id="app-user"), background_tasks=FakeBackgroundTasks(),
    )
    assert len(attempts) == 2
    assert db_session.query(AutoSyncJob).count() == 1
    assert db_session.get(HandoffCode, "handoff-code").used is True
    with pytest.raises(HTTPException) as caught:
        monday_handoff.handoff_resolve(
            HandoffResolveRequest(code="handoff-code"), db=db_session,
            current_user=CurrentUser(id="app-user"), background_tasks=FakeBackgroundTasks(),
        )
    assert caught.value.status_code == 400


@pytest.mark.parametrize("route", ["handoff", "manual"])
def test_durable_refresh_preserves_access_check(db_session, monkeypatch, route):
    task = _task("item-1")
    _handoff_fixture(db_session, task=task)
    monkeypatch.setattr(tasks, "can_read_item", lambda *args: False)
    monkeypatch.setattr(monday_handoff, "can_read_item", lambda *args: False)
    with pytest.raises(HTTPException) as caught:
        if route == "handoff":
            monday_handoff.handoff_resolve(
                HandoffResolveRequest(code="handoff-code"), db=db_session,
                current_user=CurrentUser(id="app-user"), background_tasks=FakeBackgroundTasks(),
            )
        else:
            tasks.sync_task(
                task.external_task_key, TaskSyncRequest(), db=db_session,
                current_user=CurrentUser(id="app-user"), background_tasks=FakeBackgroundTasks(),
            )
    assert caught.value.status_code == 403
    assert db_session.query(AutoSyncJob).count() == 0
    assert db_session.get(HandoffCode, "handoff-code").used is False


def test_active_reconciliation_queues_missing_stale_failed_and_stuck_items(db_session, monkeypatch):
    now = datetime.now(timezone.utc)
    fresh = _task("fresh", revision="rev-fresh")
    stale = _task("stale", revision="old-rev")
    failed = _task("failed", sync_status="failed", revision="rev-failed")
    stuck = _task("stuck", sync_status="syncing", revision="rev-stuck")
    stuck.sync_started_at = now - timedelta(hours=2)
    db_session.add_all([fresh, stale, failed, stuck])
    db_session.add(TaskSnapshot(
        id=uuid.uuid4(), external_task_key=fresh.external_task_key,
        snapshot_version="rev-fresh", task_context_json={}, ingestion_status="complete",
    ))
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


def test_active_reconciliation_rotates_across_all_groups_and_restarts(db_session, monkeypatch):
    policy = replace(_policy(), active_group_ids=frozenset({"topics", "later"}))
    groups = {"later": ["1", "2", "3"], "topics": ["4", "5", "6"]}
    checked = []
    monkeypatch.setattr(auto_sync_reconciliation, "fetch_current_account_id", lambda token: "acct")
    monkeypatch.setattr(
        auto_sync_reconciliation, "list_item_ids_in_groups",
        lambda token, board_id, group_ids, limit: groups,
    )

    def fetch_item(token, item_id, *, account_id=None):
        checked.append(item_id)
        return {
            "id": item_id, "account_id": account_id, "board": {"id": policy.board_id},
            "group": {"id": "later" if item_id in groups["later"] else "topics"},
            "updated_at": "2026-09-23T12:00:00Z", "assets": [],
        }

    monkeypatch.setattr(auto_sync_reconciliation, "fetch_current_source_revision_inputs", fetch_item)
    for _ in range(3):
        with sessionmaker(bind=db_session.bind, autoflush=False)() as restarted_session:
            result = reconcile_active_items_once(
                restarted_session, dry_run=False, access_token="token", policy=policy, limit=2,
            )
            assert result.scanned == 2
            assert result.errors == 0
    assert sorted(checked) == ["1", "2", "3", "4", "5", "6"]
    assert result.candidate_count == 6
    assert result.never_checked == 0
    assert result.max_check_age_seconds is not None
    assert db_session.query(AutoSyncReconciliationCheck).count() == 6


@pytest.fixture()
def reconciliation_source(monkeypatch):
    source = SimpleNamespace(
        groups={"topics": ["1", "2", "3"]}, failures=set(), checked=[], page_sizes=[], revisions={},
    )
    monkeypatch.setattr(auto_sync_reconciliation, "fetch_current_account_id", lambda token: "acct")

    def list_ids(token, board_id, group_ids, limit):
        source.page_sizes.append(limit)
        return source.groups

    def fetch_item(token, item_id, *, account_id=None):
        source.checked.append(item_id)
        if item_id in source.failures:
            raise RuntimeError("Source unavailable")
        return {
            "id": item_id, "account_id": account_id, "board": {"id": "1882196103"},
            "group": {"id": "topics"}, "updated_at": "2026-09-23T12:00:00Z", "assets": [],
        }

    monkeypatch.setattr(auto_sync_reconciliation, "list_item_ids_in_groups", list_ids)
    monkeypatch.setattr(auto_sync_reconciliation, "fetch_current_source_revision_inputs", fetch_item)
    monkeypatch.setattr(
        auto_sync_reconciliation, "compute_desired_source_revision",
        lambda item: source.revisions.get(item["id"], f"rev-{item['id']}"),
    )
    return source


def test_failed_checks_rotate_without_claiming_success(db_session, monkeypatch, caplog, reconciliation_source):
    caplog.set_level(logging.INFO, logger=auto_sync_reconciliation.__name__)
    source = reconciliation_source
    source.failures.add("1")
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    for minute in range(3):
        monkeypatch.setattr(auto_sync_reconciliation, "utc_now", lambda: now + timedelta(minutes=minute))
        result = reconcile_active_items_once(
            db_session, dry_run=False, access_token="token", policy=_policy(), limit=1,
        )
    assert source.checked == ["1", "2", "3"]
    assert source.page_sizes == [500, 500, 500]
    assert result.candidate_count == 3
    assert result.never_checked == 1
    assert result.max_check_age_seconds == 60
    failed = db_session.get(AutoSyncReconciliationCheck, ("1882196103", "1", "active"))
    assert failed.last_checked_at is None
    assert failed.last_outcome == "error"
    assert db_session.get(Task, "acct:1882196103:1") is None
    coverage = [record for record in caplog.records if getattr(record, "event", None) == "auto_sync.reconciliation_coverage"]
    assert coverage[-1].never_checked == 1
    source.failures.clear()
    result = reconcile_active_items_once(db_session, dry_run=False, access_token="token", policy=_policy(), limit=1)
    assert source.checked[-1] == "1"
    assert result.never_checked == 0


def test_failed_recheck_preserves_previous_success_time(db_session, monkeypatch, reconciliation_source):
    reconciliation_source.groups = {"topics": ["1"]}
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    monkeypatch.setattr(auto_sync_reconciliation, "utc_now", lambda: now)
    reconcile_active_items_once(db_session, dry_run=False, access_token="token", policy=_policy(), limit=1)
    check = db_session.get(AutoSyncReconciliationCheck, ("1882196103", "1", "active"))
    successful_check_at = check.last_checked_at
    reconciliation_source.failures.add("1")
    monkeypatch.setattr(auto_sync_reconciliation, "utc_now", lambda: now + timedelta(hours=1))
    result = reconcile_active_items_once(db_session, dry_run=False, access_token="token", policy=_policy(), limit=1)
    db_session.refresh(check)
    assert check.last_checked_at == successful_check_at
    assert check.last_attempted_at > check.last_checked_at
    assert check.last_outcome == "error"
    assert result.never_checked == 0
    assert result.max_check_age_seconds == 3600


def test_dry_run_does_not_advance_progress_or_write_jobs(db_session, reconciliation_source):
    reconciliation_source.failures.add("1")
    for _ in range(2):
        result = reconcile_active_items_once(db_session, dry_run=True, access_token="token", policy=_policy(), limit=2)
        assert result.errors == 1
        assert result.queued == 1
        assert result.never_checked == 3
        assert result.max_check_age_seconds is None
    assert reconciliation_source.checked == ["1", "2", "1", "2"]
    assert db_session.query(AutoSyncReconciliationCheck).count() == 0
    assert db_session.query(AutoSyncJob).count() == 0
    assert db_session.query(Task).count() == 0


def test_api_pagination_is_independent_of_batch_limit(db_session, monkeypatch, reconciliation_source):
    calls = []

    def graphql(token, query, variables, **kwargs):
        calls.append(variables)
        assert variables["limit"] == 2
        if "cursor" in variables:
            assert variables["cursor"] == "next-page"
            return {"data": {"next_items_page": {"items": [{"id": "3"}], "cursor": None}}}
        return {"data": {"boards": [{"groups": [{
            "id": "topics", "items_page": {"items": [{"id": "1"}, {"id": "2"}], "cursor": "next-page"},
        }]}]}}

    monkeypatch.setattr(monday_client, "monday_graphql_request", graphql)
    monkeypatch.setattr(auto_sync_reconciliation, "list_item_ids_in_groups", monday_client.list_item_ids_in_groups)
    result = reconcile_active_items_once(
        db_session, dry_run=False, access_token="token", policy=_policy(), limit=1, page_size=2,
    )
    assert len(calls) == 2
    assert result.scanned == 1
    assert result.candidate_count == 3
    assert result.never_checked == 2


@pytest.mark.parametrize("current_revision", ["rev-1", "rev-2"])
def test_running_job_only_coalesces_a_new_revision(db_session, reconciliation_source, current_revision):
    reconciliation_source.groups = {"topics": ["1"]}
    reconciliation_source.revisions["1"] = current_revision
    task = _task("1", revision="rev-1")
    db_session.add(task)
    db_session.flush()
    job, _ = coalesce_auto_sync_job(
        db_session, task, trigger_type="webhook", desired_source_revision="rev-1",
        scheduled_for=datetime.now(timezone.utc),
    )
    db_session.commit()
    auto_sync_worker.claim_due_jobs(db_session, worker_id="worker")
    task.sync_started_at = job.locked_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.commit()
    result = reconcile_active_items_once(db_session, dry_run=False, access_token="token", policy=_policy(), limit=1)
    changed = current_revision != "rev-1"
    assert result.queued == int(changed)
    assert result.items[0].refresh_reason == ("stale" if changed else "already_queued")
    assert job.desired_generation == (2 if changed else 1)
    assert job.execution_generation == 1
    assert job.desired_source_revision == current_revision
    assert job.status == "running"
    assert db_session.query(AutoSyncJob).count() == 1


def test_missing_completed_snapshot_is_not_fresh(db_session, reconciliation_source):
    reconciliation_source.groups = {"topics": ["1"]}
    db_session.add(_task("1", revision="rev-1"))
    db_session.commit()
    result = reconcile_active_items_once(db_session, dry_run=False, access_token="token", policy=_policy(), limit=1)
    assert result.items[0].action == "queued_missing_snapshot"
    assert result.items[0].refresh_reason == "missing_snapshot"


def test_progress_failure_rolls_back_enqueue_before_recording_failure(db_session, monkeypatch, reconciliation_source):
    reconciliation_source.groups = {"topics": ["1"]}
    task = _task("1", revision="old-revision")
    db_session.add(task)
    db_session.commit()
    record = auto_sync_reconciliation._record_check

    def fail_success_record(db, **kwargs):
        if kwargs["outcome"] != "error":
            raise RuntimeError("progress write failed")
        return record(db, **kwargs)

    monkeypatch.setattr(auto_sync_reconciliation, "_record_check", fail_success_record)
    result = reconcile_active_items_once(db_session, dry_run=False, access_token="token", policy=_policy(), limit=1)
    assert result.queued == 0
    assert result.errors == 1
    assert len(result.items) == 1
    assert db_session.query(AutoSyncJob).count() == 0
    assert db_session.get(AutoSyncReconciliationCheck, ("1882196103", "1", "active")).last_checked_at is None
    assert task.sync_status == "completed"


@pytest.mark.parametrize("limit,page_size", [(0, 500), (-1, 500), (1, 0), (1, 501)])
def test_invalid_reconciliation_limits_do_not_call_monday(db_session, reconciliation_source, limit, page_size):
    with pytest.raises(ValueError):
        reconcile_active_items_once(
            db_session, access_token="token", policy=_policy(), limit=limit, page_size=page_size,
        )
    assert reconciliation_source.checked == []
    assert reconciliation_source.page_sizes == []


def test_completed_transition_checks_rotate_past_still_active_tasks(db_session, monkeypatch):
    db_session.add_all([_task(item_id, revision="rev-1") for item_id in ("1", "2", "3")])
    db_session.commit()
    checked = []

    def metadata(token, item_id):
        checked.append(item_id)
        return {"id": item_id, "account_id": "acct", "board": {"id": "1882196103"}, "group": {"id": "topics"}}

    monkeypatch.setattr(auto_sync_reconciliation, "fetch_item_metadata", metadata)
    for _ in range(3):
        result = detect_completed_transitions_once(
            db_session, dry_run=False, access_token="token", policy=_policy(), limit=1,
        )
        assert result.items[0].action == "still_active"
    assert checked == ["1", "2", "3"]
    assert result.candidate_count == 3
    assert result.never_checked == 0


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
