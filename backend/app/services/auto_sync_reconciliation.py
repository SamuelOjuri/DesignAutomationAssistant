from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import AutoSyncJob, Task
from ..monday_client import (
    fetch_current_account_id,
    fetch_current_source_revision_inputs,
    fetch_item_metadata,
    list_item_ids_in_groups,
)
from .auto_sync import (
    apply_auto_sync_policy_for_item,
    compute_desired_source_revision,
    get_monday_ingestion_access_token,
    item_metadata_from_monday_item,
    utc_now,
)
from .auto_sync_policy import ACTIVE_JOB_STATUSES, AutoSyncPolicy, policy_from_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationItemResult:
    item_id: str
    group_id: Optional[str]
    external_task_key: Optional[str]
    action: str
    reason: str
    desired_source_revision: Optional[str] = None
    job_id: Optional[str] = None


@dataclass(frozen=True)
class ReconciliationResult:
    dry_run: bool
    board_id: str
    scanned: int = 0
    queued: int = 0
    skipped: int = 0
    completed_retained: int = 0
    errors: int = 0
    items: tuple[ReconciliationItemResult, ...] = field(default_factory=tuple)


def _ordered_active_group_ids(policy: AutoSyncPolicy) -> list[str]:
    return sorted(policy.active_group_ids)


def _limited_item_ids_by_group(
    item_ids_by_group: dict[str, list[str]],
    group_ids: list[str],
    limit: int,
) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    for group_id in group_ids:
        for item_id in item_ids_by_group.get(group_id, []):
            selected.append((group_id, item_id))
            if len(selected) >= limit:
                return selected
    return selected


def _has_active_job(db: Session, task: Task) -> bool:
    return (
        db.query(AutoSyncJob.id)
        .filter(
            AutoSyncJob.board_id == task.board_id,
            AutoSyncJob.item_id == task.item_id,
            AutoSyncJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .first()
        is not None
    )


def _matches_indexed_revision(task: Task, desired_source_revision: str) -> bool:
    return desired_source_revision in {
        task.last_indexed_source_revision,
        task.latest_snapshot_version,
    }


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _is_stuck_task(
    task: Task,
    *,
    has_active_job: bool,
    stuck_after_seconds: int,
) -> bool:
    if task.sync_status not in {"queued", "syncing"}:
        return False
    if not has_active_job:
        return True
    timestamp = task.sync_started_at if task.sync_status == "syncing" else task.sync_requested_at
    if timestamp is None:
        return True
    return _as_aware_utc(timestamp) <= utc_now() - timedelta(seconds=stuck_after_seconds)


def _active_reconciliation_reason(
    db: Session,
    task: Optional[Task],
    *,
    desired_source_revision: str,
    stuck_after_seconds: int,
) -> str:
    if task is None:
        return "missing"
    has_active_job = _has_active_job(db, task)
    if task.auto_sync_state == "expired":
        return "restore"
    if task.sync_status == "failed":
        return "failed"
    if _is_stuck_task(task, has_active_job=has_active_job, stuck_after_seconds=stuck_after_seconds):
        return "stuck"
    if task.sync_status in {"queued", "syncing"} and has_active_job:
        return "already_queued"
    if not _matches_indexed_revision(task, desired_source_revision):
        return "stale"
    if task.sync_status != "completed":
        return "stale"
    return "fresh"


def _desired_revision_for_job(
    task: Optional[Task],
    *,
    reconciliation_reason: str,
    desired_source_revision: str,
) -> Optional[str]:
    if task is not None and reconciliation_reason in {"failed", "stuck"}:
        if _matches_indexed_revision(task, desired_source_revision):
            return None
    return desired_source_revision


def reconcile_active_items_once(
    db: Session,
    *,
    dry_run: bool = True,
    access_token: Optional[str] = None,
    policy: Optional[AutoSyncPolicy] = None,
    limit: Optional[int] = None,
    stuck_after_seconds: int = 3600,
) -> ReconciliationResult:
    policy = policy or policy_from_settings()
    token = access_token or get_monday_ingestion_access_token()
    account_id = fetch_current_account_id(token)
    batch_limit = limit or policy.backfill_batch_size
    group_ids = _ordered_active_group_ids(policy)
    item_ids_by_group = list_item_ids_in_groups(token, policy.board_id, group_ids, limit=max(batch_limit, 1))
    selected_items = _limited_item_ids_by_group(item_ids_by_group, group_ids, batch_limit)

    item_results: list[ReconciliationItemResult] = []
    queued = 0
    skipped = 0
    errors = 0

    for expected_group_id, item_id in selected_items:
        try:
            item = fetch_current_source_revision_inputs(token, item_id, account_id=account_id)
            desired_source_revision = compute_desired_source_revision(item)
            metadata = item_metadata_from_monday_item(item, fallback_account_id=account_id)
            decision = policy.classify_group(metadata.board_id, metadata.group_id)
            task = db.get(Task, metadata.external_task_key)
            reconciliation_reason = _active_reconciliation_reason(
                db,
                task,
                desired_source_revision=desired_source_revision,
                stuck_after_seconds=stuck_after_seconds,
            )

            should_queue = decision.should_queue_sync and reconciliation_reason in {
                "missing",
                "restore",
                "failed",
                "stuck",
                "stale",
            }

            if dry_run:
                action = f"would_queue_{reconciliation_reason}" if should_queue else reconciliation_reason
                if should_queue:
                    queued += 1
                else:
                    skipped += 1
                item_results.append(
                    ReconciliationItemResult(
                        item_id=metadata.item_id,
                        group_id=metadata.group_id or expected_group_id,
                        external_task_key=metadata.external_task_key,
                        action=action,
                        reason=decision.reason,
                        desired_source_revision=desired_source_revision,
                    )
                )
                continue

            if should_queue:
                queue_result = apply_auto_sync_policy_for_item(
                    db,
                    item,
                    trigger_type="reconciliation",
                    desired_source_revision=_desired_revision_for_job(
                        task,
                        reconciliation_reason=reconciliation_reason,
                        desired_source_revision=desired_source_revision,
                    ),
                    policy=policy,
                    schedule_immediately=True,
                    fallback_account_id=account_id,
                )
                db.flush()
                job_id = str(queue_result.job.id) if queue_result.job is not None and queue_result.job.id else None
                action = f"queued_{reconciliation_reason}" if queue_result.job is not None else reconciliation_reason
                if queue_result.job is not None:
                    queued += 1
                else:
                    skipped += 1
            else:
                job_id = None
                action = reconciliation_reason
                skipped += 1

            item_results.append(
                ReconciliationItemResult(
                    item_id=metadata.item_id,
                    group_id=metadata.group_id or expected_group_id,
                    external_task_key=metadata.external_task_key,
                    action=action,
                    reason=decision.reason,
                    desired_source_revision=desired_source_revision,
                    job_id=job_id,
                )
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            errors += 1
            logger.exception("Active auto-sync reconciliation failed for item %s", item_id)
            item_results.append(
                ReconciliationItemResult(
                    item_id=str(item_id),
                    group_id=expected_group_id,
                    external_task_key=None,
                    action="error",
                    reason=str(exc),
                )
            )

    return ReconciliationResult(
        dry_run=dry_run,
        board_id=policy.board_id,
        scanned=len(selected_items),
        queued=queued,
        skipped=skipped,
        errors=errors,
        items=tuple(item_results),
    )


def detect_completed_transitions_once(
    db: Session,
    *,
    dry_run: bool = True,
    access_token: Optional[str] = None,
    policy: Optional[AutoSyncPolicy] = None,
    limit: Optional[int] = None,
    item_id: Optional[str] = None,
    external_task_key: Optional[str] = None,
) -> ReconciliationResult:
    policy = policy or policy_from_settings()
    token = access_token or get_monday_ingestion_access_token()
    query = (
        db.query(Task)
        .filter(
            Task.board_id == policy.board_id,
            Task.auto_sync_state == "active",
            Task.sync_status == "completed",
            or_(
                Task.last_indexed_source_revision.isnot(None),
                Task.latest_snapshot_version.isnot(None),
            ),
        )
        .order_by(Task.updated_at.asc())
    )
    if item_id is not None:
        query = query.filter(Task.item_id == item_id)
    if external_task_key is not None:
        query = query.filter(Task.external_task_key == external_task_key)
    if limit is not None:
        query = query.limit(limit)
    tasks = query.all()

    item_results: list[ReconciliationItemResult] = []
    completed_retained = 0
    skipped = 0
    errors = 0

    for task in tasks:
        try:
            item = fetch_item_metadata(token, task.item_id)
            item["account_id"] = task.account_id
            metadata = item_metadata_from_monday_item(item, fallback_account_id=task.account_id)
            decision = policy.classify_group(metadata.board_id, metadata.group_id)

            if decision.lifecycle_state == "completed_retained":
                action = "would_mark_completed_retained" if dry_run else "completed_retained"
                if dry_run:
                    completed_retained += 1
                else:
                    apply_auto_sync_policy_for_item(
                        db,
                        item,
                        trigger_type="reconciliation",
                        desired_source_revision=None,
                        policy=policy,
                        fallback_account_id=task.account_id,
                    )
                    completed_retained += 1
                    db.commit()
            elif decision.lifecycle_state == "excluded":
                action = "would_mark_excluded" if dry_run else "excluded"
                if not dry_run:
                    apply_auto_sync_policy_for_item(
                        db,
                        item,
                        trigger_type="reconciliation",
                        desired_source_revision=None,
                        policy=policy,
                        fallback_account_id=task.account_id,
                    )
                    db.commit()
                skipped += 1
            elif decision.lifecycle_state == "active":
                action = "still_active"
                skipped += 1
            else:
                action = "ignored"
                skipped += 1

            item_results.append(
                ReconciliationItemResult(
                    item_id=metadata.item_id,
                    group_id=metadata.group_id,
                    external_task_key=metadata.external_task_key,
                    action=action,
                    reason=decision.reason,
                )
            )
        except Exception as exc:
            db.rollback()
            errors += 1
            logger.exception("Completed-transition reconciliation failed for task %s", task.external_task_key)
            item_results.append(
                ReconciliationItemResult(
                    item_id=task.item_id,
                    group_id=task.source_group_id,
                    external_task_key=task.external_task_key,
                    action="error",
                    reason=str(exc),
                )
            )

    return ReconciliationResult(
        dry_run=dry_run,
        board_id=policy.board_id,
        scanned=len(tasks),
        skipped=skipped,
        completed_retained=completed_retained,
        errors=errors,
        items=tuple(item_results),
    )


def _run_from_new_session(args: argparse.Namespace) -> tuple[ReconciliationResult, Optional[ReconciliationResult]]:
    db = SessionLocal()
    try:
        if args.skip_active:
            policy = policy_from_settings()
            active_result = ReconciliationResult(
                dry_run=args.dry_run,
                board_id=policy.board_id,
            )
        else:
            active_result = reconcile_active_items_once(
                db,
                dry_run=args.dry_run,
                limit=args.limit,
                stuck_after_seconds=args.stuck_after_seconds,
            )
        completed_result = None
        if args.completed_transitions:
            completed_result = detect_completed_transitions_once(
                db,
                dry_run=args.dry_run,
                limit=args.completed_limit,
                item_id=args.completed_item_id,
                external_task_key=args.completed_external_task_key,
            )
        return active_result, completed_result
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile durable auto-sync jobs with monday current state")
    parser.add_argument("--limit", type=int, default=None, help="Maximum active items to inspect")
    parser.add_argument("--skip-active", action="store_true", help="Skip active-group reconciliation")
    parser.add_argument("--dry-run", action="store_true", help="Inspect monday state without creating jobs")
    parser.add_argument(
        "--stuck-after-seconds",
        type=int,
        default=3600,
        help="Age after which queued/syncing tasks are treated as stuck",
    )
    parser.add_argument(
        "--completed-transitions",
        action="store_true",
        help="Also inspect indexed active tasks for moves into the completed group",
    )
    parser.add_argument(
        "--completed-limit",
        type=int,
        default=None,
        help="Maximum indexed active tasks to inspect for completed transitions",
    )
    completed_target = parser.add_mutually_exclusive_group()
    completed_target.add_argument(
        "--completed-item-id",
        default=None,
        help="Only inspect this monday item ID for completed-transition reconciliation",
    )
    completed_target.add_argument(
        "--completed-external-task-key",
        default=None,
        help="Only inspect this external task key for completed-transition reconciliation",
    )
    args = parser.parse_args()

    active_result, completed_result = _run_from_new_session(args)
    logger.info("Active auto-sync reconciliation result: %s", active_result)
    print(active_result)
    if completed_result is not None:
        logger.info("Completed-transition auto-sync reconciliation result: %s", completed_result)
        print(completed_result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
