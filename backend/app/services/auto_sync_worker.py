from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import socket
import time
from typing import Any, Callable, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import AutoSyncJob, Task
from .auto_sync import get_monday_ingestion_access_token, utc_now

logger = logging.getLogger(__name__)

PipelineRunner = Callable[[Session, str, str, bool], Any]


def _run_sync_pipeline(db: Session, external_task_key: str, access_token: str, force: bool) -> Any:
    from .sync_pipeline import run_sync_pipeline

    return run_sync_pipeline(db, external_task_key, access_token, force)


@dataclass(frozen=True)
class WorkerRunResult:
    recovered: int = 0
    claimed: int = 0
    completed: int = 0
    skipped: int = 0
    retry_wait: int = 0
    failed: int = 0


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{settings.auto_sync_board_id}:{time.time_ns()}"


def _supports_skip_locked(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def _with_row_locks(db: Session, query):
    if _supports_skip_locked(db):
        return query.with_for_update(skip_locked=True)
    return query


def _retry_delay(attempt_count: int) -> timedelta:
    seconds = min(3600, 60 * (2 ** max(attempt_count - 1, 0)))
    return timedelta(seconds=seconds)


def _task_for_job(db: Session, job: AutoSyncJob, *, for_update: bool = False) -> Optional[Task]:
    if job.external_task_key:
        task_query = (
            db.query(Task)
            .filter(Task.external_task_key == job.external_task_key)
            .populate_existing()
        )
        if for_update and _supports_skip_locked(db):
            task_query = task_query.with_for_update()
        task = task_query.one_or_none()
        if task is not None:
            return task
    task_query = (
        db.query(Task)
        .filter(Task.board_id == job.board_id, Task.item_id == job.item_id)
        .order_by(Task.created_at.asc())
    )
    if for_update and _supports_skip_locked(db):
        task_query = task_query.with_for_update()
    return task_query.first()


def _lock_claimed_job_and_task(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
) -> tuple[Optional[AutoSyncJob], Optional[Task]]:
    job_query = (
        db.query(AutoSyncJob)
        .filter(AutoSyncJob.id == job_id)
        .populate_existing()
    )
    if _supports_skip_locked(db):
        job_query = job_query.with_for_update()
    job = job_query.one_or_none()
    if job is None or job.status != "running" or job.locked_by != worker_id:
        return job, None
    return job, _task_for_job(db, job, for_update=True)


def _mark_task_syncing(task: Task, job: AutoSyncJob, now: datetime) -> None:
    task.sync_status = "syncing"
    task.sync_started_at = now
    task.sync_completed_at = None
    task.sync_finished_at = None
    task.sync_error = None
    task.last_sync_trigger = job.trigger_type
    task.ingestion_actor = "service_token"
    task.updated_at = now


def _mark_task_completed(
    task: Task,
    *,
    result: str,
    source_revision: Optional[str],
    now: datetime,
) -> None:
    task.sync_status = "completed"
    task.sync_completed_at = now
    task.sync_finished_at = now
    task.sync_error = None
    task.last_sync_result = result
    if source_revision:
        task.last_indexed_source_revision = source_revision
    if result in {"done", "unchanged"}:
        task.auto_synced_at = now
        task.last_successful_sync_at = now
        task.ingestion_actor = "service_token"
        task.raw_purged_at = None
    task.updated_at = now


def _mark_task_failed(task: Optional[Task], *, error: str, now: datetime) -> None:
    if task is None:
        return
    task.sync_status = "failed"
    task.sync_completed_at = now
    task.sync_finished_at = now
    task.sync_error = error[:500]
    task.last_sync_result = "failed"
    task.updated_at = now


def recover_stuck_jobs(
    db: Session,
    *,
    lease_timeout_seconds: int = 3600,
    limit: int = 100,
    now: Optional[datetime] = None,
) -> int:
    now = now or utc_now()
    cutoff = now - timedelta(seconds=lease_timeout_seconds)
    query = (
        db.query(AutoSyncJob)
        .filter(
            AutoSyncJob.status == "running",
            or_(
                AutoSyncJob.locked_at.is_(None),
                AutoSyncJob.locked_at <= cutoff,
                AutoSyncJob.heartbeat_at <= cutoff,
                and_(AutoSyncJob.heartbeat_at.is_(None), AutoSyncJob.locked_at <= cutoff),
            ),
        )
        .order_by(AutoSyncJob.locked_at.asc())
        .limit(limit)
    )
    stuck_jobs = _with_row_locks(db, query).all()

    for job in stuck_jobs:
        task = _task_for_job(db, job, for_update=True)
        job.locked_at = None
        job.locked_by = None
        job.heartbeat_at = None
        job.updated_at = now
        job.last_error = "Worker lease expired"
        if job.attempt_count >= job.max_attempts:
            job.status = "failed"
            job.completed_at = now
            _mark_task_failed(task, error=job.last_error, now=now)
        else:
            job.status = "retry_wait"
            job.next_retry_at = now
            job.scheduled_for = now
            _mark_task_failed(task, error=job.last_error, now=now)

    if stuck_jobs:
        db.commit()
    return len(stuck_jobs)


def claim_due_jobs(
    db: Session,
    *,
    worker_id: str,
    limit: int = 1,
    now: Optional[datetime] = None,
) -> list[AutoSyncJob]:
    now = now or utc_now()
    query = (
        db.query(AutoSyncJob)
        .filter(
            or_(
                and_(AutoSyncJob.status.in_(("pending", "scheduled")), AutoSyncJob.scheduled_for <= now),
                and_(
                    AutoSyncJob.status == "retry_wait",
                    or_(AutoSyncJob.next_retry_at.is_(None), AutoSyncJob.next_retry_at <= now),
                ),
            )
        )
        .order_by(AutoSyncJob.scheduled_for.asc(), AutoSyncJob.created_at.asc())
        .limit(limit)
    )
    due_jobs = _with_row_locks(db, query).all()

    for job in due_jobs:
        job.status = "running"
        job.locked_at = now
        job.locked_by = worker_id
        job.heartbeat_at = now
        job.started_at = now
        job.attempt_count = (job.attempt_count or 0) + 1
        job.updated_at = now
        task = _task_for_job(db, job, for_update=True)
        if task is not None:
            job.external_task_key = task.external_task_key
            _mark_task_syncing(task, job, now)

    if due_jobs:
        db.commit()
    return due_jobs


def heartbeat_job(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    now: Optional[datetime] = None,
) -> bool:
    now = now or utc_now()
    job = db.get(AutoSyncJob, job_id)
    if job is None or job.status != "running" or job.locked_by != worker_id:
        return False
    job.heartbeat_at = now
    job.updated_at = now
    db.commit()
    return True


def _finish_job(
    job: AutoSyncJob,
    *,
    status: str,
    now: datetime,
    error: Optional[str] = None,
) -> None:
    job.status = status
    job.completed_at = now
    job.locked_at = None
    job.locked_by = None
    job.heartbeat_at = None
    job.next_retry_at = None
    job.last_error = error
    job.updated_at = now


def execute_claimed_job(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    access_token: Optional[str] = None,
    pipeline_runner: PipelineRunner = _run_sync_pipeline,
    force: bool = False,
    now: Optional[datetime] = None,
) -> str:
    now = now or utc_now()
    job = db.get(AutoSyncJob, job_id)
    if job is None:
        return "missing"
    if job.status != "running" or job.locked_by != worker_id:
        return "not_claimed"

    task = _task_for_job(db, job)
    if task is None:
        job, task = _lock_claimed_job_and_task(db, job_id, worker_id=worker_id)
        if job is None:
            db.rollback()
            return "missing"
        if job.status != "running" or job.locked_by != worker_id:
            db.rollback()
            return "not_claimed"
        if task is not None:
            db.rollback()
            task = _task_for_job(db, job)
        else:
            _finish_job(job, status="failed", now=now, error="Task not found for auto-sync job")
            db.commit()
            return "failed"

    if job.desired_source_revision and job.desired_source_revision == task.last_indexed_source_revision:
        job, task = _lock_claimed_job_and_task(db, job_id, worker_id=worker_id)
        if job is None:
            db.rollback()
            return "missing"
        if job.status != "running" or job.locked_by != worker_id:
            db.rollback()
            return "not_claimed"
        if task is None:
            _finish_job(job, status="failed", now=now, error="Task not found for auto-sync job")
            db.commit()
            return "failed"
        if not (
            job.desired_source_revision
            and job.desired_source_revision == task.last_indexed_source_revision
        ):
            db.rollback()
            job = db.get(AutoSyncJob, job_id)
            task = _task_for_job(db, job) if job is not None else None
            if job is None or task is None:
                return "missing"
        else:
            _finish_job(job, status="skipped", now=now)
            _mark_task_completed(task, result="skipped", source_revision=job.desired_source_revision, now=now)
            db.commit()
            return "skipped"

    if task is None:
        _finish_job(job, status="failed", now=now, error="Task not found for auto-sync job")
        db.commit()
        return "failed"

    job_id_for_retry = job.id
    try:
        token = access_token or get_monday_ingestion_access_token()
        result = pipeline_runner(db, task.external_task_key, token, force)
        finished_at = utc_now()
        job, task = _lock_claimed_job_and_task(db, job_id, worker_id=worker_id)
        if job is None:
            db.rollback()
            return "missing"
        if job.status != "running" or job.locked_by != worker_id:
            db.rollback()
            return "not_claimed"
        if task is None:
            _finish_job(job, status="failed", now=finished_at, error="Task not found for auto-sync job")
            db.commit()
            return "failed"
        source_revision = result.snapshot_version or job.desired_source_revision
        result_status = "unchanged" if result.status == "unchanged" else "done"
        _finish_job(job, status="completed", now=finished_at)
        _mark_task_completed(task, result=result_status, source_revision=source_revision, now=finished_at)
        db.commit()
        return "completed"
    except Exception as exc:
        db.rollback()
        failed_at = utc_now()
        job, task = _lock_claimed_job_and_task(
            db,
            job_id_for_retry,
            worker_id=worker_id,
        )
        if job is None:
            db.rollback()
            return "missing"
        if job.status != "running" or job.locked_by != worker_id:
            db.rollback()
            return "not_claimed"
        error = str(exc)[:1000]
        job.locked_at = None
        job.locked_by = None
        job.heartbeat_at = None
        job.last_error = error
        job.updated_at = failed_at
        _mark_task_failed(task, error=error, now=failed_at)
        if job.attempt_count < job.max_attempts:
            job.status = "retry_wait"
            job.next_retry_at = failed_at + _retry_delay(job.attempt_count)
            job.scheduled_for = job.next_retry_at
            db.commit()
            logger.exception("Auto-sync job %s failed; retry scheduled", job_id)
            return "retry_wait"

        job.status = "failed"
        job.completed_at = failed_at
        db.commit()
        logger.exception("Auto-sync job %s failed permanently", job_id)
        return "failed"


def run_due_jobs_once(
    db: Session,
    *,
    worker_id: Optional[str] = None,
    limit: int = 1,
    lease_timeout_seconds: int = 3600,
    access_token: Optional[str] = None,
    pipeline_runner: PipelineRunner = _run_sync_pipeline,
) -> WorkerRunResult:
    worker_id = worker_id or default_worker_id()
    recovered = recover_stuck_jobs(db, lease_timeout_seconds=lease_timeout_seconds)
    claimed_jobs = claim_due_jobs(db, worker_id=worker_id, limit=limit)

    counts = {"completed": 0, "skipped": 0, "retry_wait": 0, "failed": 0}
    for job in claimed_jobs:
        status = execute_claimed_job(
            db,
            job.id,
            worker_id=worker_id,
            access_token=access_token,
            pipeline_runner=pipeline_runner,
        )
        if status in counts:
            counts[status] += 1

    return WorkerRunResult(
        recovered=recovered,
        claimed=len(claimed_jobs),
        completed=counts["completed"],
        skipped=counts["skipped"],
        retry_wait=counts["retry_wait"],
        failed=counts["failed"],
    )


def _run_once_from_new_session(args: argparse.Namespace, worker_id: str) -> WorkerRunResult:
    db = SessionLocal()
    try:
        return run_due_jobs_once(
            db,
            worker_id=worker_id,
            limit=args.limit,
            lease_timeout_seconds=args.lease_timeout_seconds,
        )
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the durable auto-sync worker")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit")
    parser.add_argument("--limit", type=int, default=1, help="Maximum jobs to claim per batch")
    parser.add_argument("--poll-seconds", type=float, default=10.0, help="Delay between batches in loop mode")
    parser.add_argument("--lease-timeout-seconds", type=int, default=3600, help="Running job lease timeout")
    parser.add_argument("--worker-id", default=None, help="Stable worker id for lock ownership")
    parser.add_argument("--ignore-disabled", action="store_true", help="Run even when AUTO_SYNC_WORKER_ENABLED=false")
    args = parser.parse_args()

    if not settings.auto_sync_worker_enabled and not args.ignore_disabled:
        logger.warning("Auto-sync worker disabled by AUTO_SYNC_WORKER_ENABLED")
        return 0

    worker_id = args.worker_id or default_worker_id()
    while True:
        result = _run_once_from_new_session(args, worker_id)
        logger.info("Auto-sync worker batch: %s", result)
        if args.once:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())