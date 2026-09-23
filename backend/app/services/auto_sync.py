from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Optional
import uuid

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AutoSyncJob, Task, TaskSnapshot
from ..monday_client import fetch_current_source_revision_inputs
from .auto_sync_policy import (
    ACTIVE_JOB_STATUSES,
    AutoSyncDecision,
    AutoSyncPolicy,
    build_external_task_key,
    policy_from_settings,
)
from .db_retry import AutoSyncConcurrencyError
from .storage_ingest import compute_snapshot_version

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ItemMetadata:
    account_id: str
    board_id: str
    item_id: str
    group_id: Optional[str]
    group_title: Optional[str]
    external_task_key: str


@dataclass(frozen=True)
class QueueResult:
    task: Optional[Task]
    job: Optional[AutoSyncJob]
    decision: AutoSyncDecision
    created_task: bool = False
    created_job: bool = False


def get_monday_ingestion_access_token() -> str:
    access_token = settings.monday_ingestion_access_token
    if not access_token:
        raise HTTPException(status_code=503, detail="MONDAY_INGESTION_ACCESS_TOKEN is not configured")
    return access_token


def compute_desired_source_revision(item: dict[str, Any]) -> str:
    return compute_snapshot_version(item)


def fetch_desired_source_revision(item_id: str, access_token: Optional[str] = None) -> str:
    token = access_token or get_monday_ingestion_access_token()
    item = fetch_current_source_revision_inputs(token, item_id)
    return compute_desired_source_revision(item)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def has_completed_snapshot(db: Session, task: Task, revision: Optional[str]) -> bool:
    return bool(revision) and task.auto_sync_state != "expired" and (
        db.query(TaskSnapshot.id).filter_by(
            external_task_key=task.external_task_key,
            snapshot_version=revision, ingestion_status="complete",
        ).first() is not None
    )


def refresh_reason_for_task(
    db: Session, task: Optional[Task], *, desired_source_revision: Optional[str], force: bool = False,
) -> str:
    if force:
        return "force"
    if task is None:
        return "missing"
    if task.auto_sync_state == "expired":
        return "restore"
    if desired_source_revision is None:
        return "source_revision_unknown"
    if task.sync_status == "failed":
        return "failed"
    if not task.last_indexed_source_revision and not task.latest_snapshot_version:
        return "missing_snapshot"
    if desired_source_revision not in {task.last_indexed_source_revision, task.latest_snapshot_version}:
        return "stale"
    return "fresh" if has_completed_snapshot(db, task, desired_source_revision) else "missing_snapshot"


def log_refresh_decision(
    *, external_task_key: str, trigger_type: str, action: str, reason: str,
    desired_source_revision: Optional[str] = None, indexed_source_revision: Optional[str] = None,
    force: bool = False, job: Optional[AutoSyncJob] = None,
) -> None:
    fields = dict(
        event="auto_sync.refresh_decision", external_task_key=external_task_key,
        sync_trigger=trigger_type, refresh_action=action, refresh_reason=reason,
        desired_source_revision=desired_source_revision, indexed_source_revision=indexed_source_revision,
        force=force, job_id=str(job.id) if job is not None else None,
        desired_generation=job.desired_generation if job is not None else None,
        execution_generation=job.execution_generation if job is not None else None,
        execution_source_revision=job.execution_source_revision if job is not None else None,
    )
    logger.info(
        "Refresh decision task=%s trigger=%s action=%s reason=%s desired_revision=%s indexed_revision=%s "
        "force=%s job=%s desired_generation=%s execution_generation=%s execution_revision=%s",
        external_task_key, trigger_type, action, reason, desired_source_revision, indexed_source_revision,
        force, fields["job_id"], fields["desired_generation"], fields["execution_generation"],
        fields["execution_source_revision"], extra=fields,
    )


def _supports_row_locks(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def _active_jobs_query(db: Session, *, board_id: str, item_id: str):
    query = (
        db.query(AutoSyncJob)
        .filter(
            AutoSyncJob.board_id == str(board_id),
            AutoSyncJob.item_id == str(item_id),
            AutoSyncJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .populate_existing()
        .order_by(AutoSyncJob.created_at.asc(), AutoSyncJob.id.asc())
    )
    return query.with_for_update() if _supports_row_locks(db) else query


def lock_auto_sync_state(
    db: Session,
    *,
    board_id: str,
    item_id: str,
    external_task_key: str,
) -> tuple[list[AutoSyncJob], Optional[Task]]:
    active_jobs = _active_jobs_query(db, board_id=board_id, item_id=item_id).all()
    task_query = db.query(Task).filter(Task.external_task_key == external_task_key).populate_existing()
    if _supports_row_locks(db):
        task_query = task_query.with_for_update()
    task = task_query.one_or_none()
    return active_jobs, task


def item_metadata_from_monday_item(
    item: dict[str, Any],
    *,
    fallback_account_id: Optional[str] = None,
) -> ItemMetadata:
    board = item.get("board") or {}
    group = item.get("group") or {}
    account = item.get("account") or board.get("account") or {}

    account_id = str(account.get("id") or item.get("account_id") or fallback_account_id or "")
    board_id = str(board.get("id") or item.get("board_id") or "")
    item_id = str(item.get("id") or item.get("item_id") or "")
    group_id = group.get("id") or item.get("group_id")
    group_title = group.get("title") or item.get("group_title")

    if not account_id or not board_id or not item_id:
        raise HTTPException(status_code=502, detail="monday item metadata missing account, board, or item id")

    return ItemMetadata(
        account_id=account_id,
        board_id=board_id,
        item_id=item_id,
        group_id=str(group_id) if group_id is not None else None,
        group_title=str(group_title) if group_title is not None else None,
        external_task_key=build_external_task_key(account_id, board_id, item_id),
    )


def upsert_auto_sync_task(
    db: Session,
    metadata: ItemMetadata,
    *,
    lifecycle_state: str,
    now: Optional[datetime] = None,
    existing_task: Optional[Task] = None,
    task_lookup_complete: bool = False,
) -> tuple[Task, bool]:
    now = now or utc_now()
    task = existing_task
    if not task_lookup_complete:
        task = db.get(Task, metadata.external_task_key)
    created = False
    if task is None:
        task = Task(
            external_task_key=metadata.external_task_key,
            account_id=metadata.account_id,
            board_id=metadata.board_id,
            item_id=metadata.item_id,
        )
        try:
            with db.begin_nested():
                db.add(task)
                db.flush([task])
            created = True
        except IntegrityError:
            task_query = db.query(Task).filter(Task.external_task_key == metadata.external_task_key)
            if task_lookup_complete and _supports_row_locks(db):
                task_query = task_query.with_for_update()
            task = task_query.one_or_none()
            if task is None:
                raise

    task.auto_sync_enabled = True
    task.auto_sync_state = lifecycle_state
    task.source_group_id = metadata.group_id
    task.source_group_title = metadata.group_title
    if lifecycle_state == "active":
        task.completed_at = None
        task.purge_after = None
        task.raw_purged_at = None

    task.updated_at = now
    return task, created


def mark_task_queued(
    task: Task,
    *,
    trigger_type: str,
    desired_source_revision: Optional[str],
    now: Optional[datetime] = None,
) -> None:
    now = now or utc_now()
    task.sync_status = "queued"
    task.sync_requested_at = now
    task.sync_completed_at = None
    task.sync_finished_at = None
    task.sync_error = None
    task.last_sync_trigger = trigger_type
    if desired_source_revision:
        task.last_sync_result = None


def coalesce_auto_sync_job(
    db: Session,
    task: Task,
    *,
    trigger_type: str,
    desired_source_revision: Optional[str] = None,
    scheduled_for: Optional[datetime] = None,
    debounce_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
    existing_job: Optional[AutoSyncJob] = None,
    active_job_lookup_complete: bool = False,
    force: bool = False,
    refresh_reason: Optional[str] = None,
) -> tuple[AutoSyncJob, bool]:
    now = now or utc_now()
    refresh_reason = refresh_reason or refresh_reason_for_task(
        db, task, desired_source_revision=desired_source_revision, force=force,
    )
    if scheduled_for is None:
        delay = debounce_seconds if debounce_seconds is not None else policy_from_settings().debounce_seconds
        scheduled_for = now + timedelta(seconds=delay)

    job = existing_job
    if not active_job_lookup_complete:
        job = _active_jobs_query(
            db,
            board_id=task.board_id,
            item_id=task.item_id,
        ).first()
    created = False
    was_running = False
    if job is None:
        candidate_job = AutoSyncJob(
            id=uuid.uuid4(),
            board_id=task.board_id,
            item_id=task.item_id,
            external_task_key=task.external_task_key,
            trigger_type=trigger_type,
            desired_source_revision=desired_source_revision,
            desired_generation=1,
            force_requested=force,
            status="scheduled",
            scheduled_for=scheduled_for,
            attempt_count=0,
            max_attempts=3,
            created_at=now,
            updated_at=now,
        )
        try:
            with db.begin_nested():
                db.add(candidate_job)
                db.flush([candidate_job])
            job = candidate_job
            created = True
        except IntegrityError as exc:
            if active_job_lookup_complete:
                raise AutoSyncConcurrencyError(
                    "An active auto-sync job was created concurrently"
                ) from exc
            job = _active_jobs_query(
                db,
                board_id=task.board_id,
                item_id=task.item_id,
            ).first()
            if job is None:
                raise

    if job is not None and not created:
        was_running = job.status == "running"
        user_requested = job.trigger_type in {"manual", "handoff", "restore"}
        job.external_task_key = task.external_task_key
        if was_running or not user_requested or trigger_type in {"manual", "handoff", "restore"}:
            job.trigger_type = trigger_type
        job.desired_generation += 1
        job.force_requested = job.force_requested or force
        job.desired_source_revision = (
            desired_source_revision if job.desired_source_revision is not None else None
        )
        if job.status != "running":
            job.status = "scheduled"
            if not user_requested or trigger_type in {"manual", "handoff", "restore"}:
                job.scheduled_for = scheduled_for
            job.next_retry_at = None
            job.locked_at = None
            job.locked_by = None
            job.heartbeat_at = None
        job.updated_at = now
    else:
        was_running = False

    if was_running:
        task.sync_requested_at = now
        task.last_sync_trigger = trigger_type
        if desired_source_revision:
            task.last_sync_result = None
        task.updated_at = now
    else:
        mark_task_queued(task, trigger_type=job.trigger_type, desired_source_revision=desired_source_revision, now=now)
    log_refresh_decision(
        external_task_key=task.external_task_key, trigger_type=trigger_type,
        action="queued" if created else "coalesced_running" if was_running else "coalesced",
        reason=refresh_reason, desired_source_revision=desired_source_revision,
        indexed_source_revision=task.last_indexed_source_revision, force=force, job=job,
    )
    return job, created


def enqueue_user_refresh(
    db: Session,
    *,
    account_id: str,
    board_id: str,
    item_id: str,
    trigger_type: str,
    desired_source_revision: Optional[str] = None,
    force: bool = False,
    refresh_reason: Optional[str] = None,
) -> tuple[Task, AutoSyncJob]:
    from .auto_sync_purge import mark_expired_task_restoring, record_meaningful_access

    if not settings.auto_sync_worker_enabled:
        raise HTTPException(status_code=503, detail="The durable sync worker is disabled")
    get_monday_ingestion_access_token()
    external_task_key = build_external_task_key(account_id, board_id, item_id)
    jobs, task = lock_auto_sync_state(
        db, board_id=board_id, item_id=item_id, external_task_key=external_task_key,
    )
    refresh_reason = "force" if force else refresh_reason or refresh_reason_for_task(
        db, task, desired_source_revision=desired_source_revision,
    )
    if task is None:
        task = Task(
            external_task_key=external_task_key, account_id=account_id,
            board_id=board_id, item_id=item_id,
        )
        try:
            with db.begin_nested():
                db.add(task)
                db.flush([task])
        except IntegrityError as exc:
            raise AutoSyncConcurrencyError("Task was created concurrently") from exc
    record_meaningful_access(db, task)
    mark_expired_task_restoring(db, task)
    job, _ = coalesce_auto_sync_job(
        db, task, trigger_type=trigger_type,
        desired_source_revision=desired_source_revision,
        scheduled_for=utc_now(), force=force,
        existing_job=jobs[0] if jobs else None, active_job_lookup_complete=True,
        refresh_reason=refresh_reason,
    )
    return task, job


def cancel_active_auto_sync_jobs(
    db: Session,
    *,
    board_id: str,
    item_id: str,
    reason: str,
    now: Optional[datetime] = None,
    existing_jobs: Optional[list[AutoSyncJob]] = None,
    active_job_lookup_complete: bool = False,
) -> int:
    now = now or utc_now()
    jobs = existing_jobs
    if not active_job_lookup_complete:
        jobs = _active_jobs_query(db, board_id=board_id, item_id=item_id).all()
    jobs = jobs or []
    for job in jobs:
        job.status = "cancelled"
        job.completed_at = now
        job.last_error = reason
        job.locked_at = None
        job.locked_by = None
        job.heartbeat_at = None
        job.updated_at = now
    return len(jobs)


def apply_auto_sync_policy_for_item(
    db: Session,
    item: dict[str, Any],
    *,
    trigger_type: str,
    desired_source_revision: Optional[str] = None,
    policy: Optional[AutoSyncPolicy] = None,
    now: Optional[datetime] = None,
    schedule_immediately: bool = False,
    fallback_account_id: Optional[str] = None,
    refresh_reason: Optional[str] = None,
) -> QueueResult:
    now = now or utc_now()
    policy = policy or policy_from_settings()
    metadata = item_metadata_from_monday_item(item, fallback_account_id=fallback_account_id)
    decision = policy.classify_group(metadata.board_id, metadata.group_id)

    if not decision.should_track_task:
        log_refresh_decision(
            external_task_key=metadata.external_task_key, trigger_type=trigger_type,
            action="ignored", reason=decision.reason, desired_source_revision=desired_source_revision,
        )
        return QueueResult(task=None, job=None, decision=decision)

    active_jobs, task = lock_auto_sync_state(
        db,
        board_id=metadata.board_id,
        item_id=metadata.item_id,
        external_task_key=metadata.external_task_key,
    )
    created_task = False
    job = None
    created_job = False

    if decision.requires_existing_index and task is None:
        log_refresh_decision(
            external_task_key=metadata.external_task_key, trigger_type=trigger_type,
            action="ignored", reason="no_existing_index", desired_source_revision=desired_source_revision,
        )
        return QueueResult(task=None, job=None, decision=decision)

    refresh_reason = refresh_reason or refresh_reason_for_task(db, task, desired_source_revision=desired_source_revision)
    if task is None or decision.lifecycle_state in {"active", "excluded"}:
        task, created_task = upsert_auto_sync_task(
            db,
            metadata,
            lifecycle_state=decision.lifecycle_state or "active",
            now=now,
            existing_task=task,
            task_lookup_complete=True,
        )
    elif decision.lifecycle_state == "completed_retained":
        task.auto_sync_state = "completed_retained"
        task.source_group_id = metadata.group_id
        task.source_group_title = metadata.group_title
        if task.completed_at is None:
            task.completed_at = now
        task.purge_after = policy.purge_after_for(task.completed_at)
        task.updated_at = now

    if decision.should_cancel_active_jobs:
        cancel_active_auto_sync_jobs(
            db,
            board_id=metadata.board_id,
            item_id=metadata.item_id,
            reason=decision.reason,
            now=now,
            existing_jobs=active_jobs,
            active_job_lookup_complete=True,
        )

    if (
        decision.should_queue_sync
        and task is not None
        and desired_source_revision
        and task.last_indexed_source_revision == desired_source_revision
        and not active_jobs
        and task.auto_sync_state != "expired"
        and has_completed_snapshot(db, task, desired_source_revision)
    ):
        task.sync_status = "completed"
        task.sync_finished_at = now
        task.sync_completed_at = now
        task.sync_error = None
        task.last_sync_trigger = trigger_type
        task.last_sync_result = "skipped"
        task.updated_at = now
        log_refresh_decision(
            external_task_key=task.external_task_key, trigger_type=trigger_type, action="skipped", reason="fresh",
            desired_source_revision=desired_source_revision, indexed_source_revision=task.last_indexed_source_revision,
        )
        return QueueResult(task=task, job=None, decision=decision, created_task=created_task)

    if decision.should_queue_sync and task is not None:
        job, created_job = coalesce_auto_sync_job(
            db,
            task,
            trigger_type=trigger_type,
            desired_source_revision=desired_source_revision,
            scheduled_for=now if schedule_immediately else None,
            debounce_seconds=policy.debounce_seconds,
            now=now,
            existing_job=active_jobs[0] if active_jobs else None,
            active_job_lookup_complete=True,
            refresh_reason=refresh_reason,
        )
    else:
        log_refresh_decision(
            external_task_key=metadata.external_task_key, trigger_type=trigger_type,
            action="lifecycle_only", reason=decision.reason, desired_source_revision=desired_source_revision,
        )

    return QueueResult(task=task, job=job, decision=decision, created_task=created_task, created_job=created_job)