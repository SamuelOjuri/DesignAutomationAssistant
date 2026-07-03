from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AutoSyncJob, Task
from ..monday_client import fetch_current_source_revision_inputs
from .auto_sync_policy import (
    ACTIVE_JOB_STATUSES,
    AutoSyncDecision,
    AutoSyncPolicy,
    build_external_task_key,
    policy_from_settings,
)
from .storage_ingest import compute_snapshot_version


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
) -> tuple[Task, bool]:
    now = now or utc_now()
    task = db.get(Task, metadata.external_task_key)
    created = False
    if task is None:
        task = Task(
            external_task_key=metadata.external_task_key,
            account_id=metadata.account_id,
            board_id=metadata.board_id,
            item_id=metadata.item_id,
        )
        db.add(task)
        created = True

    task.auto_sync_enabled = True
    task.auto_sync_state = lifecycle_state
    task.source_group_id = metadata.group_id
    task.source_group_title = metadata.group_title
    if lifecycle_state == "active":
        task.completed_at = None
        task.purge_after = None

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
) -> tuple[AutoSyncJob, bool]:
    now = now or utc_now()
    if scheduled_for is None:
        delay = debounce_seconds if debounce_seconds is not None else policy_from_settings().debounce_seconds
        scheduled_for = now + timedelta(seconds=delay)

    job = (
        db.query(AutoSyncJob)
        .filter(
            AutoSyncJob.board_id == task.board_id,
            AutoSyncJob.item_id == task.item_id,
            AutoSyncJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(AutoSyncJob.created_at.asc())
        .first()
    )
    created = False
    was_running = False
    if job is None:
        job = AutoSyncJob(
            id=uuid.uuid4(),
            board_id=task.board_id,
            item_id=task.item_id,
            external_task_key=task.external_task_key,
            trigger_type=trigger_type,
            desired_source_revision=desired_source_revision,
            status="scheduled",
            scheduled_for=scheduled_for,
            attempt_count=0,
            max_attempts=3,
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        created = True
    else:
        was_running = job.status == "running"
        job.external_task_key = task.external_task_key
        job.trigger_type = trigger_type
        job.desired_source_revision = desired_source_revision or job.desired_source_revision
        if job.status != "running":
            job.status = "scheduled"
            job.scheduled_for = scheduled_for
            job.next_retry_at = None
            job.locked_at = None
            job.locked_by = None
            job.heartbeat_at = None
        job.updated_at = now

    if was_running:
        task.sync_requested_at = now
        task.last_sync_trigger = trigger_type
        if desired_source_revision:
            task.last_sync_result = None
        task.updated_at = now
    else:
        mark_task_queued(task, trigger_type=trigger_type, desired_source_revision=desired_source_revision, now=now)
    return job, created


def cancel_active_auto_sync_jobs(
    db: Session,
    *,
    board_id: str,
    item_id: str,
    reason: str,
    now: Optional[datetime] = None,
) -> int:
    now = now or utc_now()
    jobs = (
        db.query(AutoSyncJob)
        .filter(
            AutoSyncJob.board_id == str(board_id),
            AutoSyncJob.item_id == str(item_id),
            AutoSyncJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .all()
    )
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
) -> QueueResult:
    now = now or utc_now()
    policy = policy or policy_from_settings()
    metadata = item_metadata_from_monday_item(item, fallback_account_id=fallback_account_id)
    decision = policy.classify_group(metadata.board_id, metadata.group_id)

    if not decision.should_track_task:
        return QueueResult(task=None, job=None, decision=decision)

    task = db.get(Task, metadata.external_task_key)
    created_task = False
    job = None
    created_job = False

    if decision.requires_existing_index and task is None:
        return QueueResult(task=None, job=None, decision=decision)

    if task is None or decision.lifecycle_state in {"active", "excluded"}:
        task, created_task = upsert_auto_sync_task(
            db,
            metadata,
            lifecycle_state=decision.lifecycle_state or "active",
            now=now,
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
        )

    if (
        decision.should_queue_sync
        and task is not None
        and desired_source_revision
        and task.last_indexed_source_revision == desired_source_revision
    ):
        task.sync_status = "completed"
        task.sync_finished_at = now
        task.sync_completed_at = now
        task.sync_error = None
        task.last_sync_trigger = trigger_type
        task.last_sync_result = "skipped"
        task.updated_at = now
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
        )

    return QueueResult(task=task, job=job, decision=decision, created_task=created_task, created_job=created_job)