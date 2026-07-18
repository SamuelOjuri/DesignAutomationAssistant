from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from tenacity import stop_after_attempt, wait_none

from backend.app import monday_client
from backend.app.db import Base
from backend.app.models import AutoSyncJob, Task
from backend.app.services import auto_sync_worker
from backend.app.services.auto_sync import coalesce_auto_sync_job
from backend.app.services.auto_sync_backfill import active_backfill_once
from backend.app.services.auto_sync_policy import AutoSyncPolicy
from backend.app.services.auto_sync_worker import WorkerRunResult, claim_due_jobs, execute_claimed_job, run_due_jobs_once


@dataclass
class FakeSyncResult:
    status: str
    snapshot_version: str | None


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


def _task(item_id: str = "item-1") -> Task:
    return Task(
        external_task_key=f"acct:1882196103:{item_id}",
        account_id="acct",
        board_id="1882196103",
        item_id=item_id,
        auto_sync_enabled=True,
        auto_sync_state="active",
    )


def test_source_revision_query_uses_account_fallback_not_board_account(monkeypatch):
    calls = []

    def fake_graphql_request(access_token, query, variables=None, *, timeout=10, allow_unauthorized=False):
        calls.append(query)
        return {
            "data": {
                "items": [
                    {
                        "id": "1",
                        "name": "Test item",
                        "updated_at": "2026-07-01T12:00:00Z",
                        "board": {"id": "1882196103", "name": "Design queue"},
                        "group": {"id": "topics", "title": "Hub A - Outstanding"},
                        "assets": [],
                        "column_values": [],
                        "updates": [],
                    }
                ]
            }
        }

    monkeypatch.setattr(monday_client, "monday_graphql_request", fake_graphql_request)

    item = monday_client.fetch_current_source_revision_inputs("token", "1", account_id="acct")

    assert "account" not in calls[0]
    assert item["account_id"] == "acct"


def test_fetch_current_account_id_reads_me_account(monkeypatch):
    monkeypatch.setattr(
        monday_client,
        "monday_graphql_request",
        lambda access_token, query, variables=None, *, timeout=10, allow_unauthorized=False: {
            "data": {"me": {"account": {"id": "acct"}}}
        },
    )

    assert monday_client.fetch_current_account_id("token") == "acct"


def test_monday_graphql_request_retries_transient_status(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.ok = 200 <= status_code < 300

        def json(self):
            return self._payload

    responses = [
        FakeResponse(502),
        FakeResponse(200, {"data": {"ok": True}}),
    ]

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(
        monday_client,
        "_post_monday_graphql",
        monday_client._post_monday_graphql.retry_with(wait=wait_none()),
    )

    payload = monday_client.monday_graphql_request("token", "query { ok }")

    assert payload == {"data": {"ok": True}}
    assert len(calls) == 2


def test_monday_graphql_request_reports_transient_failure_after_retries(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 502
        ok = False

        def json(self):
            return {}

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(
        monday_client,
        "_post_monday_graphql",
        monday_client._post_monday_graphql.retry_with(
            stop=stop_after_attempt(2),
            wait=wait_none(),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        monday_client.monday_graphql_request("token", "query { ok }")

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "monday API error (502)"
    assert len(calls) == 2


def test_worker_runs_due_job_and_updates_task_state(db_session):
    now = datetime.now(timezone.utc)
    task = _task()
    db_session.add(task)
    db_session.flush()
    job, _ = coalesce_auto_sync_job(
        db_session,
        task,
        trigger_type="backfill",
        desired_source_revision="rev-2",
        scheduled_for=now,
        now=now,
    )
    db_session.commit()

    calls = []

    def fake_pipeline(db, external_task_key, access_token, force):
        calls.append((external_task_key, access_token, force))
        return FakeSyncResult(status="done", snapshot_version="rev-2")

    result = run_due_jobs_once(
        db_session,
        worker_id="worker-1",
        access_token="service-token",
        pipeline_runner=fake_pipeline,
    )

    db_session.refresh(task)
    db_session.refresh(job)

    assert result.claimed == 1
    assert result.completed == 1
    assert calls == [(task.external_task_key, "service-token", False)]
    assert job.status == "completed"
    assert job.locked_at is None
    assert job.locked_by is None
    assert task.sync_status == "completed"
    assert task.last_sync_result == "done"
    assert task.last_sync_trigger == "backfill"
    assert task.last_indexed_source_revision == "rev-2"
    assert task.last_successful_sync_at is not None


def test_worker_skips_job_when_source_revision_is_fresh(db_session):
    now = datetime.now(timezone.utc)
    task = _task()
    task.last_indexed_source_revision = "rev-1"
    db_session.add(task)
    db_session.flush()
    job, _ = coalesce_auto_sync_job(
        db_session,
        task,
        trigger_type="reconciliation",
        desired_source_revision="rev-1",
        scheduled_for=now,
        now=now,
    )
    db_session.commit()

    def fake_pipeline(db, external_task_key, access_token, force):
        raise AssertionError("fresh jobs should not run ingestion")

    result = run_due_jobs_once(
        db_session,
        worker_id="worker-1",
        access_token="service-token",
        pipeline_runner=fake_pipeline,
    )

    db_session.refresh(task)
    db_session.refresh(job)

    assert result.claimed == 1
    assert result.skipped == 1
    assert job.status == "skipped"
    assert task.sync_status == "completed"
    assert task.last_sync_result == "skipped"
    assert task.last_indexed_source_revision == "rev-1"


def test_worker_retries_failed_job_without_losing_durable_state(db_session):
    now = datetime.now(timezone.utc)
    task = _task()
    db_session.add(task)
    db_session.flush()
    job, _ = coalesce_auto_sync_job(
        db_session,
        task,
        trigger_type="backfill",
        desired_source_revision="rev-3",
        scheduled_for=now,
        now=now,
    )
    job.max_attempts = 3
    db_session.commit()

    def failing_pipeline(db, external_task_key, access_token, force):
        raise RuntimeError("temporary monday throttling")

    result = run_due_jobs_once(
        db_session,
        worker_id="worker-1",
        access_token="service-token",
        pipeline_runner=failing_pipeline,
    )

    db_session.refresh(task)
    db_session.refresh(job)
    assert result.claimed == 1
    assert result.retry_wait == 1
    assert job.status == "retry_wait"
    assert job.attempt_count == 1
    assert job.next_retry_at is not None
    assert job.locked_at is None
    assert task.sync_status == "failed"
    assert task.last_sync_result == "failed"


def test_worker_batch_continues_after_transient_database_error(monkeypatch):
    sleep_calls = []

    def failing_run_once(args, worker_id):
        raise OperationalError(
            "SELECT 1",
            {},
            RuntimeError("connection refused"),
            connection_invalidated=True,
        )

    monkeypatch.setattr(auto_sync_worker, "_run_once_from_new_session", failing_run_once)
    monkeypatch.setattr(auto_sync_worker.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    args = argparse.Namespace(
        once=False,
        db_error_backoff_seconds=12.5,
        limit=1,
        lease_timeout_seconds=3600,
    )

    assert auto_sync_worker._run_worker_batch(args, "worker-1") is None
    assert sleep_calls == [12.5]


def test_run_once_ignores_database_error_during_session_close(monkeypatch):
    class ClosingSession:
        invalidated = False

        def close(self):
            raise OperationalError(
                "ROLLBACK",
                {},
                RuntimeError("connection aborted"),
                connection_invalidated=True,
            )

        def invalidate(self):
            self.invalidated = True

    session = ClosingSession()
    monkeypatch.setattr(auto_sync_worker, "SessionLocal", lambda: session)
    monkeypatch.setattr(auto_sync_worker, "run_due_jobs_once", lambda *args, **kwargs: WorkerRunResult())

    args = argparse.Namespace(limit=1, lease_timeout_seconds=3600)

    assert auto_sync_worker._run_once_from_new_session(args, "worker-1") == WorkerRunResult()
    assert session.invalidated is True


def test_worker_does_not_finalize_job_cancelled_during_pipeline(db_session):
    now = datetime.now(timezone.utc)
    task = _task()
    db_session.add(task)
    db_session.flush()
    job, _ = coalesce_auto_sync_job(
        db_session,
        task,
        trigger_type="backfill",
        desired_source_revision="rev-cancelled",
        scheduled_for=now,
        now=now,
    )
    db_session.commit()

    def cancelling_pipeline(db, external_task_key, access_token, force):
        running_job = db.get(AutoSyncJob, job.id)
        running_job.status = "cancelled"
        running_job.locked_at = None
        running_job.locked_by = None
        running_job.heartbeat_at = None
        db.commit()
        return FakeSyncResult(status="done", snapshot_version="rev-cancelled")

    claimed = claim_due_jobs(db_session, worker_id="worker-1", now=now)
    status = execute_claimed_job(
        db_session,
        claimed[0].id,
        worker_id="worker-1",
        access_token="service-token",
        pipeline_runner=cancelling_pipeline,
    )

    db_session.refresh(task)
    db_session.refresh(job)
    assert status == "not_claimed"
    assert job.status == "cancelled"
    assert task.last_indexed_source_revision is None


def test_active_backfill_dry_run_lists_only_configured_active_groups(db_session, monkeypatch):
    policy = AutoSyncPolicy(
        enabled=True,
        board_id="1882196103",
        active_group_ids=frozenset({"topics", "group_mkpbs35c", "group_mkqbx92r"}),
        excluded_group_ids=frozenset({"group_mkpbd6vy"}),
        completed_group_id="group_mkpbb3tx",
        retention_days=30,
        debounce_seconds=90,
        backfill_batch_size=10,
    )
    requested = {}

    def fake_list_item_ids(token, board_id, group_ids, limit):
        requested["board_id"] = board_id
        requested["group_ids"] = tuple(group_ids)
        return {
            "topics": ["1"],
            "group_mkpbs35c": ["2"],
            "group_mkqbx92r": ["3"],
            "group_mkpbb3tx": ["completed-should-not-appear"],
        }

    def fake_fetch_revision_inputs(token, item_id, *, account_id=None):
        return {
            "id": item_id,
            "updated_at": f"2026-07-01T12:00:0{item_id}Z",
            "account_id": account_id,
            "board": {"id": "1882196103"},
            "group": {"id": "topics", "title": "Hub A - Outstanding"},
            "assets": [],
            "updates": [],
        }

    monkeypatch.setattr(
        "backend.app.services.auto_sync_backfill.list_item_ids_in_groups",
        fake_list_item_ids,
    )
    monkeypatch.setattr(
        "backend.app.services.auto_sync_backfill.fetch_current_source_revision_inputs",
        fake_fetch_revision_inputs,
    )
    monkeypatch.setattr(
        "backend.app.services.auto_sync_backfill.fetch_current_account_id",
        lambda token: "acct",
    )

    result = active_backfill_once(db_session, dry_run=True, access_token="service-token", policy=policy)

    assert requested["board_id"] == "1882196103"
    assert set(requested["group_ids"]) == {"topics", "group_mkpbs35c", "group_mkqbx92r"}
    assert "group_mkpbb3tx" not in requested["group_ids"]
    assert result.dry_run is True
    assert result.scanned == 3
    assert result.queued == 3
    assert db_session.query(AutoSyncJob).count() == 0
    assert {item.action for item in result.items} == {"would_queue"}


def test_active_backfill_queues_small_batch_immediately(db_session, monkeypatch):
    policy = AutoSyncPolicy(
        enabled=True,
        board_id="1882196103",
        active_group_ids=frozenset({"topics", "group_mkpbs35c", "group_mkqbx92r"}),
        excluded_group_ids=frozenset({"group_mkpbd6vy"}),
        completed_group_id="group_mkpbb3tx",
        retention_days=30,
        debounce_seconds=90,
        backfill_batch_size=1,
    )

    def fake_list_item_ids(token, board_id, group_ids, limit):
        return {"topics": ["1"], "group_mkpbs35c": ["2"], "group_mkqbx92r": ["3"]}

    def fake_fetch_revision_inputs(token, item_id, *, account_id=None):
        return {
            "id": item_id,
            "updated_at": f"2026-07-01T12:00:0{item_id}Z",
            "account_id": account_id,
            "board": {"id": "1882196103"},
            "group": {"id": "topics", "title": "Hub A - Outstanding"},
            "assets": [],
            "updates": [],
        }

    monkeypatch.setattr(
        "backend.app.services.auto_sync_backfill.list_item_ids_in_groups",
        fake_list_item_ids,
    )
    monkeypatch.setattr(
        "backend.app.services.auto_sync_backfill.fetch_current_source_revision_inputs",
        fake_fetch_revision_inputs,
    )
    monkeypatch.setattr(
        "backend.app.services.auto_sync_backfill.fetch_current_account_id",
        lambda token: "acct",
    )

    result = active_backfill_once(db_session, dry_run=False, access_token="service-token", policy=policy)

    tasks = db_session.query(Task).all()
    jobs = db_session.query(AutoSyncJob).all()
    assert result.scanned == 1
    assert result.queued == 1
    assert len(tasks) == 1
    assert len(jobs) == 1
    assert tasks[0].sync_status == "queued"
    assert tasks[0].auto_sync_state == "active"
    assert jobs[0].status == "scheduled"
    assert jobs[0].scheduled_for is not None


def test_active_backfill_skips_already_indexed_revision(db_session, monkeypatch):
    policy = AutoSyncPolicy(
        enabled=True,
        board_id="1882196103",
        active_group_ids=frozenset({"topics"}),
        excluded_group_ids=frozenset({"group_mkpbd6vy"}),
        completed_group_id="group_mkpbb3tx",
        retention_days=30,
        debounce_seconds=90,
        backfill_batch_size=10,
    )
    item = {
        "id": "1",
        "updated_at": "2026-07-01T12:00:01Z",
        "account_id": "acct",
        "board": {"id": "1882196103"},
        "group": {"id": "topics", "title": "Hub A - Outstanding"},
        "assets": [],
        "updates": [],
    }
    existing_task = _task("1")
    existing_task.last_indexed_source_revision = "latest-revision"
    db_session.add(existing_task)
    db_session.commit()

    monkeypatch.setattr(
        "backend.app.services.auto_sync_backfill.list_item_ids_in_groups",
        lambda token, board_id, group_ids, limit: {"topics": ["1"]},
    )
    monkeypatch.setattr(
        "backend.app.services.auto_sync_backfill.fetch_current_source_revision_inputs",
        lambda token, item_id, *, account_id=None: item,
    )
    monkeypatch.setattr(
        "backend.app.services.auto_sync_backfill.fetch_current_account_id",
        lambda token: "acct",
    )
    monkeypatch.setattr(
        "backend.app.services.auto_sync_backfill.compute_desired_source_revision",
        lambda fetched_item: "latest-revision",
    )

    result = active_backfill_once(db_session, dry_run=False, access_token="service-token", policy=policy)

    db_session.refresh(existing_task)
    assert result.scanned == 1
    assert result.queued == 0
    assert result.skipped == 1
    assert db_session.query(AutoSyncJob).count() == 0
    assert existing_task.sync_status == "completed"
    assert existing_task.last_sync_result == "skipped"