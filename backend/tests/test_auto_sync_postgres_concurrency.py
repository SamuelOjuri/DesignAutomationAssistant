from __future__ import annotations

from datetime import datetime, timezone
import os
from queue import Queue
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.models import AutoSyncJob, Task
from backend.app.services.auto_sync import apply_auto_sync_policy_for_item
from backend.app.services.auto_sync_policy import AutoSyncPolicy


@pytest.fixture()
def postgres_session_factory():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL concurrency tests")

    schema = f"auto_sync_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    test_engine = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        test_engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args={"options": f"-csearch_path={schema},public"},
        )
        Task.__table__.create(test_engine)
        AutoSyncJob.__table__.create(test_engine)
        yield sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    finally:
        if test_engine is not None:
            test_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def _wait_until_postgres_session_is_blocked(observer, backend_pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        wait_event_type = observer.execute(
            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
            {"pid": backend_pid},
        ).scalar_one_or_none()
        observer.commit()
        if wait_event_type == "Lock":
            return
        time.sleep(0.01)
    raise AssertionError("Webhook database session did not block on the worker lock")


def test_webhook_locks_job_before_task(postgres_session_factory):
    Session = postgres_session_factory
    now = datetime.now(timezone.utc)
    external_task_key = "acct:board:item"
    policy = AutoSyncPolicy(
        enabled=True,
        board_id="board",
        active_group_ids=frozenset({"active"}),
        excluded_group_ids=frozenset(),
        completed_group_id="completed",
        retention_days=30,
        debounce_seconds=90,
        backfill_batch_size=10,
    )
    item = {
        "id": "item",
        "account_id": "acct",
        "board": {"id": "board"},
        "group": {"id": "active", "title": "Active"},
    }

    setup = Session()
    job_id = uuid.uuid4()
    task = Task(
        external_task_key=external_task_key,
        account_id="acct",
        board_id="board",
        item_id="item",
        auto_sync_enabled=True,
        auto_sync_state="active",
        sync_status="queued",
    )
    job = AutoSyncJob(
        id=job_id,
        board_id="board",
        item_id="item",
        external_task_key=external_task_key,
        trigger_type="webhook",
        desired_source_revision="rev-1",
        status="scheduled",
        scheduled_for=now,
        attempt_count=0,
        max_attempts=3,
        created_at=now,
        updated_at=now,
    )
    setup.add_all([task, job])
    setup.commit()
    setup.close()

    worker = Session()
    observer = Session()
    worker_job = (
        worker.query(AutoSyncJob)
        .filter(AutoSyncJob.id == job_id)
        .with_for_update()
        .one()
    )

    backend_pids: Queue[int] = Queue()
    outcomes: Queue[BaseException | str] = Queue()

    def run_webhook_transaction() -> None:
        webhook = Session()
        try:
            webhook.execute(text("SET statement_timeout = '5s'"))
            backend_pids.put(webhook.execute(text("SELECT pg_backend_pid()")).scalar_one())
            result = apply_auto_sync_policy_for_item(
                webhook,
                item,
                trigger_type="webhook",
                desired_source_revision="rev-2",
                policy=policy,
            )
            webhook.commit()
            outcomes.put(str(result.job.id) if result.job is not None else "missing")
        except BaseException as exc:
            webhook.rollback()
            outcomes.put(exc)
        finally:
            webhook.close()

    thread = threading.Thread(target=run_webhook_transaction, daemon=True)
    thread.start()
    backend_pid = backend_pids.get(timeout=2)

    worker_error: BaseException | None = None
    try:
        _wait_until_postgres_session_is_blocked(observer, backend_pid)
        worker.execute(text("SET LOCAL lock_timeout = '500ms'"))
        worker_task = (
            worker.query(Task)
            .filter(Task.external_task_key == external_task_key)
            .with_for_update()
            .one()
        )
        worker_job.status = "running"
        worker_job.locked_by = "worker-1"
        worker_job.locked_at = now
        worker_task.sync_status = "syncing"
        worker.commit()
    except BaseException as exc:
        worker_error = exc
    finally:
        if worker.in_transaction():
            worker.rollback()
        worker.close()
        observer.close()

    thread.join(timeout=5)
    assert not thread.is_alive()
    if worker_error is not None:
        raise worker_error
    outcome = outcomes.get(timeout=1)
    if isinstance(outcome, BaseException):
        raise outcome

    verify = Session()
    try:
        persisted_job = verify.query(AutoSyncJob).one()
        persisted_task = verify.get(Task, external_task_key)
        assert outcome == str(persisted_job.id)
        assert persisted_job.status == "running"
        assert persisted_job.desired_source_revision == "rev-2"
        assert persisted_task.sync_status == "syncing"
        assert persisted_task.last_sync_trigger == "webhook"
    finally:
        verify.close()