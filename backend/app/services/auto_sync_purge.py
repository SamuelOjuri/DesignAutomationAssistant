from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Callable, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Task, TaskChunk, TaskFile, TaskSnapshot
from ..supabase_client import supabase
from .auto_sync import utc_now
from .auto_sync_policy import AutoSyncPolicy, policy_from_settings

logger = logging.getLogger(__name__)

PURGE_IN_PROGRESS_STATES = ("purge_pending", "storage_deleting", "database_cleaning")
PURGE_ELIGIBLE_STATES = ("completed_retained", *PURGE_IN_PROGRESS_STATES)

StorageObjectRemover = Callable[[str, str], None]


@dataclass(frozen=True)
class PurgeTaskResult:
    external_task_key: str
    action: str
    reason: str
    files_deleted: int = 0
    files_failed: int = 0
    chunks_deleted: int = 0
    task_files_deleted: int = 0
    snapshots_deleted: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class PurgeRunResult:
    dry_run: bool
    disabled: bool = False
    scanned: int = 0
    purged: int = 0
    skipped: int = 0
    failed: int = 0
    items: tuple[PurgeTaskResult, ...] = field(default_factory=tuple)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=utc_now().tzinfo)
    return value


def _query_datetime(db: Session, value: datetime) -> datetime:
    if db.bind is not None and db.bind.dialect.name == "sqlite" and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _default_remove_storage_object(bucket: str, object_path: str) -> None:
    supabase.storage.from_(bucket).remove([object_path])


def record_meaningful_access(
    db: Session,
    task: Task,
    *,
    policy: Optional[AutoSyncPolicy] = None,
    now: Optional[datetime] = None,
) -> None:
    now = now or utc_now()
    policy = policy or policy_from_settings()
    task.last_meaningful_access_at = now
    if task.auto_sync_state == "completed_retained" and task.retention_hold is not True:
        task.purge_after = policy.purge_after_for(now)
    task.updated_at = now
    db.add(task)


def place_retention_hold(
    db: Session,
    task: Task,
    *,
    held_by: Optional[str],
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    now = now or utc_now()
    task.retention_hold = True
    task.retention_hold_at = now
    task.retention_hold_by = held_by
    task.retention_hold_reason = reason
    record_meaningful_access(db, task, now=now)


def remove_retention_hold(
    db: Session,
    task: Task,
    *,
    now: Optional[datetime] = None,
) -> None:
    now = now or utc_now()
    task.retention_hold = False
    task.retention_hold_at = None
    task.retention_hold_by = None
    task.retention_hold_reason = None
    task.updated_at = now
    db.add(task)


def mark_expired_task_restoring(
    db: Session,
    task: Task,
    *,
    now: Optional[datetime] = None,
) -> None:
    now = now or utc_now()
    if task.auto_sync_state != "expired":
        return
    task.auto_sync_state = "completed_retained"
    if task.completed_at is None:
        task.completed_at = now
    task.purge_after = policy_from_settings().purge_after_for(now)
    task.raw_purged_at = None
    task.updated_at = now
    db.add(task)


def _is_due_for_purge(task: Task, now: datetime) -> bool:
    if task.retention_hold is True:
        return False
    if task.auto_sync_state not in PURGE_ELIGIBLE_STATES:
        return False
    if task.purge_after is None:
        return False
    return _as_aware_utc(task.purge_after) <= now


def _eligible_tasks(
    db: Session,
    *,
    policy: AutoSyncPolicy,
    now: datetime,
    limit: int,
) -> list[Task]:
    filters = [
        Task.board_id == policy.board_id,
        Task.auto_sync_state.in_(PURGE_ELIGIBLE_STATES),
        Task.retention_hold.isnot(True),
        Task.purge_after.isnot(None),
    ]
    if db.bind is None or db.bind.dialect.name != "sqlite":
        filters.append(Task.purge_after <= _query_datetime(db, now))

    return (
        db.query(Task)
        .filter(*filters)
        .order_by(Task.purge_after.asc(), Task.updated_at.asc())
        .limit(limit)
        .all()
    )


def _delete_storage_objects(
    db: Session,
    task: Task,
    *,
    remove_storage_object: StorageObjectRemover,
    now: datetime,
) -> tuple[int, int]:
    files = (
        db.query(TaskFile)
        .filter(
            TaskFile.external_task_key == task.external_task_key,
            TaskFile.deleted_at.is_(None),
        )
        .order_by(TaskFile.created_at.asc())
        .all()
    )
    deleted_count = 0
    failed_count = 0

    for file_record in files:
        try:
            remove_storage_object(file_record.bucket, file_record.object_path)
        except Exception as storage_error:
            failed_count += 1
            file_record.delete_error = str(storage_error)[:1000]
            logger.exception("Failed to delete Storage object for task %s", task.external_task_key)
        else:
            deleted_count += 1
            file_record.deleted_at = now
            file_record.delete_error = None

    return deleted_count, failed_count


def _clean_database_rows(db: Session, task: Task) -> tuple[int, int, int]:
    file_ids = [
        row[0]
        for row in db.query(TaskFile.id)
        .filter(TaskFile.external_task_key == task.external_task_key)
        .all()
    ]
    chunks_deleted = 0
    if file_ids:
        chunks_deleted = (
            db.query(TaskChunk)
            .filter(TaskChunk.file_id.in_(file_ids))
            .delete(synchronize_session=False)
        )

    task_files_deleted = (
        db.query(TaskFile)
        .filter(TaskFile.external_task_key == task.external_task_key)
        .delete(synchronize_session=False)
    )
    snapshots_deleted = (
        db.query(TaskSnapshot)
        .filter(TaskSnapshot.external_task_key == task.external_task_key)
        .delete(synchronize_session=False)
    )
    return chunks_deleted, task_files_deleted, snapshots_deleted


def purge_task_heavy_data(
    db: Session,
    task: Task,
    *,
    remove_storage_object: StorageObjectRemover = _default_remove_storage_object,
    now: Optional[datetime] = None,
) -> PurgeTaskResult:
    now = now or utc_now()
    if not _is_due_for_purge(task, now):
        return PurgeTaskResult(
            external_task_key=task.external_task_key,
            action="skipped",
            reason="not_due",
        )

    task.auto_sync_state = "purge_pending"
    task.sync_error = None
    task.updated_at = now
    db.flush()

    task.auto_sync_state = "storage_deleting"
    files_deleted, files_failed = _delete_storage_objects(
        db,
        task,
        remove_storage_object=remove_storage_object,
        now=now,
    )
    if files_failed:
        task.sync_error = f"Purge failed to delete {files_failed} Storage object(s)"
        task.updated_at = now
        return PurgeTaskResult(
            external_task_key=task.external_task_key,
            action="storage_failed",
            reason="storage_delete_failed",
            files_deleted=files_deleted,
            files_failed=files_failed,
            error=task.sync_error,
        )

    task.auto_sync_state = "database_cleaning"
    task.updated_at = now
    db.flush()

    chunks_deleted, task_files_deleted, snapshots_deleted = _clean_database_rows(db, task)

    task.auto_sync_state = "expired"
    task.raw_purged_at = now
    task.latest_snapshot_version = None
    task.sync_status = "completed"
    task.sync_finished_at = now
    task.sync_error = None
    task.last_sync_result = "skipped"
    task.updated_at = now

    return PurgeTaskResult(
        external_task_key=task.external_task_key,
        action="expired",
        reason="purged",
        files_deleted=files_deleted,
        chunks_deleted=chunks_deleted,
        task_files_deleted=task_files_deleted,
        snapshots_deleted=snapshots_deleted,
    )


def purge_expired_tasks_once(
    db: Session,
    *,
    dry_run: bool = True,
    limit: int = 25,
    policy: Optional[AutoSyncPolicy] = None,
    remove_storage_object: StorageObjectRemover = _default_remove_storage_object,
    ignore_disabled: bool = False,
    now: Optional[datetime] = None,
) -> PurgeRunResult:
    now = now or utc_now()
    policy = policy or policy_from_settings()
    if not settings.auto_sync_purge_enabled and not ignore_disabled:
        return PurgeRunResult(dry_run=dry_run, disabled=True)

    tasks = _eligible_tasks(db, policy=policy, now=now, limit=limit)
    items: list[PurgeTaskResult] = []
    purged = 0
    skipped = 0
    failed = 0

    for task in tasks:
        if not _is_due_for_purge(task, now):
            skipped += 1
            items.append(
                PurgeTaskResult(
                    external_task_key=task.external_task_key,
                    action="skipped",
                    reason="not_due",
                )
            )
            continue

        if dry_run:
            items.append(
                PurgeTaskResult(
                    external_task_key=task.external_task_key,
                    action="would_purge",
                    reason="due",
                )
            )
            purged += 1
            continue

        try:
            item_result = purge_task_heavy_data(
                db,
                task,
                remove_storage_object=remove_storage_object,
                now=now,
            )
            if item_result.action == "expired":
                purged += 1
            elif item_result.action == "storage_failed":
                failed += 1
            else:
                skipped += 1
            items.append(item_result)
            db.commit()
        except Exception as purge_error:
            db.rollback()
            failed += 1
            logger.exception("Failed to purge task %s", task.external_task_key)
            items.append(
                PurgeTaskResult(
                    external_task_key=task.external_task_key,
                    action="failed",
                    reason="exception",
                    error=str(purge_error)[:1000],
                )
            )

    return PurgeRunResult(
        dry_run=dry_run,
        scanned=len(tasks),
        purged=purged,
        skipped=skipped,
        failed=failed,
        items=tuple(items),
    )


def _run_once_from_new_session(args: argparse.Namespace) -> PurgeRunResult:
    db = SessionLocal()
    try:
        return purge_expired_tasks_once(
            db,
            dry_run=args.dry_run,
            limit=args.limit,
            ignore_disabled=args.ignore_disabled,
        )
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run completed-task auto-sync purge once")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ignore-disabled", action="store_true")
    args = parser.parse_args()

    result = _run_once_from_new_session(args)
    print(
        "auto-sync purge: "
        f"disabled={result.disabled} scanned={result.scanned} purged={result.purged} "
        f"skipped={result.skipped} failed={result.failed} dry_run={result.dry_run}"
    )
    for item in result.items:
        print(
            f"{item.external_task_key}\t{item.action}\t{item.reason}\t"
            f"files_deleted={item.files_deleted}\tfiles_failed={item.files_failed}\t"
            f"chunks_deleted={item.chunks_deleted}\t"
            f"task_files_deleted={item.task_files_deleted}\t"
            f"snapshots_deleted={item.snapshots_deleted}"
        )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())