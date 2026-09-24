from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import AutoSyncJob, AutoSyncReconciliationCheck, Task
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
    has_completed_snapshot,
    item_metadata_from_monday_item,
    log_refresh_decision,
    utc_now,
)
from .auto_sync_policy import ACTIVE_JOB_STATUSES, AutoSyncPolicy, policy_from_settings
from .monday_metadata import enqueue_metadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationItemResult:
    item_id: str
    group_id: Optional[str]
    external_task_key: Optional[str]
    action: str
    reason: str
    refresh_reason: Optional[str] = None
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
    candidate_count: int = 0
    never_checked: int = 0
    oldest_checked_at: Optional[datetime] = None
    max_check_age_seconds: Optional[float] = None
    source_unavailable: int = 0


def _ordered_active_group_ids(policy: AutoSyncPolicy) -> list[str]:
    return sorted(policy.active_group_ids)


def _fair_item_ids_by_group(
    db: Session,
    item_ids_by_group: dict[str, list[str]],
    group_ids: list[str],
    limit: int,
    *,
    board_id: str,
    scope: str = "active",
) -> list[tuple[str, str]]:
    checks = {
        check.item_id: _as_aware_utc(check.last_attempted_at)
        for check in db.query(AutoSyncReconciliationCheck).populate_existing().filter_by(board_id=board_id, scope=scope).all()
    }
    candidates: dict[str, str] = {}
    for group_id in group_ids:
        for item_id in item_ids_by_group.get(group_id, []):
            candidates.setdefault(str(item_id), group_id)
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(candidates, key=lambda item_id: (checks.get(item_id, oldest), item_id))
    return [(candidates[item_id], item_id) for item_id in ordered[:limit]]


def _record_check(
    db: Session, *, board_id: str, item_id: str, scope: str, outcome: str, reason: str,
) -> None:
    now = utc_now()
    values = dict(
        board_id=board_id, item_id=item_id, scope=scope,
        last_attempted_at=now, last_outcome=outcome, last_reason=reason,
    )
    if outcome not in {"error", "source_unavailable"}:
        values["last_checked_at"] = now
    insert = postgres_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    statement = insert(AutoSyncReconciliationCheck).values(**values)
    db.execute(statement.on_conflict_do_update(
        index_elements=["board_id", "item_id", "scope"],
        set_={key: value for key, value in values.items() if key not in {"board_id", "item_id", "scope"}},
    ))


def _coverage(
    db: Session, *, board_id: str, scope: str, item_ids: set[str], dry_run: bool,
) -> dict:
    checked_at = {
        check.item_id: _as_aware_utc(check.last_checked_at)
        for check in db.query(AutoSyncReconciliationCheck).populate_existing().filter_by(
            board_id=board_id, scope=scope,
        ).all()
        if check.item_id in item_ids and check.last_checked_at is not None
    }
    oldest = min(checked_at.values(), default=None)
    metrics = dict(
        candidate_count=len(item_ids), never_checked=len(item_ids) - len(checked_at),
        oldest_checked_at=oldest,
        max_check_age_seconds=max(0.0, (utc_now() - oldest).total_seconds()) if oldest else None,
    )
    logger.info(
        "Reconciliation coverage board=%s scope=%s candidates=%s never_checked=%s max_check_age_seconds=%s dry_run=%s",
        board_id, scope, metrics["candidate_count"], metrics["never_checked"],
        metrics["max_check_age_seconds"], dry_run,
        extra={"event": "auto_sync.reconciliation_coverage", "board_id": board_id,
               "reconciliation_scope": scope, "dry_run": dry_run, **metrics},
    )
    return metrics


def _active_job(db: Session, task: Task) -> Optional[AutoSyncJob]:
    return (
        db.query(AutoSyncJob)
        .populate_existing()
        .filter(
            AutoSyncJob.board_id == task.board_id,
            AutoSyncJob.item_id == task.item_id,
            AutoSyncJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .first()
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
    job: Optional[AutoSyncJob],
    stuck_after_seconds: int,
) -> bool:
    if task.sync_status not in {"queued", "syncing"}:
        return False
    if job is None:
        return True
    timestamp = (
        job.heartbeat_at or job.locked_at or task.sync_started_at
        if job.status == "running" else job.next_retry_at or job.scheduled_for
    )
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
    job = _active_job(db, task)
    if task.auto_sync_state == "expired":
        return "restore"
    if task.sync_status == "failed":
        return "failed"
    if _is_stuck_task(task, job=job, stuck_after_seconds=stuck_after_seconds):
        return "stuck"
    if job is not None:
        if job.desired_source_revision is not None and job.desired_source_revision != desired_source_revision:
            return "stale"
        return "already_queued"
    if not _matches_indexed_revision(task, desired_source_revision):
        return "stale"
    if not has_completed_snapshot(db, task, desired_source_revision):
        return "missing_snapshot"
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
    page_size: int = 500,
    stuck_after_seconds: int = 3600,
) -> ReconciliationResult:
    policy = policy or policy_from_settings()
    batch_limit = policy.backfill_batch_size if limit is None else limit
    if batch_limit < 1 or not 1 <= page_size <= 500:
        raise ValueError("Reconciliation limit must be positive and page_size must be between 1 and 500")
    token = access_token or get_monday_ingestion_access_token()
    account_id = fetch_current_account_id(token)
    group_ids = _ordered_active_group_ids(policy)
    item_ids_by_group = list_item_ids_in_groups(token, policy.board_id, group_ids, limit=page_size)
    selected_items = _fair_item_ids_by_group(
        db, item_ids_by_group, group_ids, batch_limit, board_id=policy.board_id,
    )

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
            if not decision.should_queue_sync:
                reconciliation_reason = decision.reason

            should_queue = decision.should_queue_sync and reconciliation_reason in {
                "missing",
                "restore",
                "failed",
                "stuck",
                "stale",
                "missing_snapshot",
            }

            if dry_run:
                action = f"would_queue_{reconciliation_reason}" if should_queue else reconciliation_reason
                if should_queue:
                    queued += 1
                else:
                    skipped += 1
                log_refresh_decision(
                    external_task_key=metadata.external_task_key, trigger_type="reconciliation",
                    action=action, reason=reconciliation_reason, desired_source_revision=desired_source_revision,
                    indexed_source_revision=task.last_indexed_source_revision if task is not None else None,
                )
                item_results.append(
                    ReconciliationItemResult(
                        item_id=metadata.item_id,
                        group_id=metadata.group_id or expected_group_id,
                        external_task_key=metadata.external_task_key,
                        action=action,
                        reason=decision.reason,
                        refresh_reason=reconciliation_reason,
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
                    refresh_reason=reconciliation_reason,
                )
                db.flush()
                job_id = str(queue_result.job.id) if queue_result.job is not None and queue_result.job.id else None
                action = f"queued_{reconciliation_reason}" if queue_result.job is not None else reconciliation_reason
                if queue_result.job is None:
                    action = reconciliation_reason = "metadata_queued" if queue_result.metadata_queued else "fresh"
            else:
                job_id = None
                action = reconciliation_reason
                if decision.should_queue_sync and task is not None:
                    enqueue_metadata(db, task, immediate=True, only_if_idle=True)

            _record_check(
                db, board_id=policy.board_id, item_id=item_id, scope="active",
                outcome=action, reason=reconciliation_reason,
            )
            db.commit()
            queued += int(job_id is not None)
            skipped += int(job_id is None)
            log_refresh_decision(
                external_task_key=metadata.external_task_key, trigger_type="reconciliation",
                action=action, reason=reconciliation_reason, desired_source_revision=desired_source_revision,
                indexed_source_revision=task.last_indexed_source_revision if task is not None else None,
            )
            item_results.append(
                ReconciliationItemResult(
                    item_id=metadata.item_id,
                    group_id=metadata.group_id or expected_group_id,
                    external_task_key=metadata.external_task_key,
                    action=action,
                    reason=decision.reason,
                    refresh_reason=reconciliation_reason,
                    desired_source_revision=desired_source_revision,
                    job_id=job_id,
                )
            )
        except Exception as exc:
            db.rollback()
            errors += 1
            logger.exception("Active auto-sync reconciliation failed for item %s", item_id)
            log_refresh_decision(
                external_task_key=f"{account_id}:{policy.board_id}:{item_id}", trigger_type="reconciliation",
                action="error", reason="check_failed",
            )
            if not dry_run:
                _record_check(
                    db, board_id=policy.board_id, item_id=item_id, scope="active",
                    outcome="error", reason=type(exc).__name__,
                )
                db.commit()
            item_results.append(
                ReconciliationItemResult(
                    item_id=str(item_id),
                    group_id=expected_group_id,
                    external_task_key=None,
                    action="error",
                    reason=str(exc),
                    refresh_reason="check_failed",
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
        **_coverage(
            db, board_id=policy.board_id, scope="active", dry_run=dry_run,
            item_ids={str(item_id) for group_id in group_ids for item_id in item_ids_by_group.get(group_id, [])},
        ),
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
    if limit is not None and limit < 1:
        raise ValueError("Completed-transition limit must be positive")
    token = access_token or get_monday_ingestion_access_token()
    query = (
        db.query(Task)
        .outerjoin(AutoSyncReconciliationCheck, and_(
            AutoSyncReconciliationCheck.board_id == Task.board_id,
            AutoSyncReconciliationCheck.item_id == Task.item_id,
            AutoSyncReconciliationCheck.scope == "completed_transition",
        ))
        .filter(
            Task.board_id == policy.board_id,
            Task.auto_sync_state == "active",
            Task.sync_status == "completed",
            or_(
                Task.last_indexed_source_revision.isnot(None),
                Task.latest_snapshot_version.isnot(None),
            ),
        )
        .order_by(AutoSyncReconciliationCheck.last_attempted_at.asc().nullsfirst(), Task.item_id.asc())
    )
    if item_id is not None:
        query = query.filter(Task.item_id == item_id)
    if external_task_key is not None:
        query = query.filter(Task.external_task_key == external_task_key)
    candidate_ids = {row.item_id for row in query.with_entities(Task.item_id).all()}
    if limit is not None:
        query = query.limit(limit)
    tasks = query.all()

    item_results: list[ReconciliationItemResult] = []
    completed_retained = 0
    skipped = 0
    errors = 0
    source_unavailable = 0

    for task in tasks:
        try:
            try:
                item = fetch_item_metadata(token, task.item_id)
            except HTTPException as exc:
                if exc.status_code != 404 or exc.detail != "monday item not found":
                    raise
                if not dry_run:
                    _record_check(
                        db, board_id=policy.board_id, item_id=task.item_id, scope="completed_transition",
                        outcome="source_unavailable", reason="source_unavailable",
                    )
                    db.commit()
                source_unavailable += 1
                skipped += 1
                logger.warning(
                    "Completed-transition source unavailable for task %s; retaining stored data (dry_run=%s)",
                    task.external_task_key, dry_run,
                    extra={"event": "auto_sync.source_unavailable", "external_task_key": task.external_task_key,
                           "board_id": policy.board_id, "item_id": task.item_id, "dry_run": dry_run},
                )
                log_refresh_decision(
                    external_task_key=task.external_task_key, trigger_type="reconciliation",
                    action="source_unavailable", reason="source_unavailable",
                    indexed_source_revision=task.last_indexed_source_revision,
                )
                item_results.append(ReconciliationItemResult(
                    item_id=task.item_id, group_id=task.source_group_id,
                    external_task_key=task.external_task_key, action="source_unavailable",
                    reason="source_unavailable", refresh_reason="source_unavailable",
                ))
                continue
            item["account_id"] = task.account_id
            metadata = item_metadata_from_monday_item(item, fallback_account_id=task.account_id)
            decision = policy.classify_group(metadata.board_id, metadata.group_id)

            if decision.lifecycle_state == "completed_retained":
                action = "would_mark_completed_retained" if dry_run else "completed_retained"
                if not dry_run:
                    apply_auto_sync_policy_for_item(
                        db,
                        item,
                        trigger_type="reconciliation",
                        desired_source_revision=None,
                        policy=policy,
                        fallback_account_id=task.account_id,
                    )
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
            elif decision.lifecycle_state == "active":
                action = "still_active"
            else:
                action = "ignored"

            if not dry_run:
                _record_check(
                    db, board_id=policy.board_id, item_id=task.item_id, scope="completed_transition",
                    outcome=action, reason=decision.reason,
                )
                db.commit()
            completed_retained += int(decision.lifecycle_state == "completed_retained")
            skipped += int(decision.lifecycle_state != "completed_retained")
            log_refresh_decision(
                external_task_key=metadata.external_task_key, trigger_type="reconciliation",
                action=action, reason=decision.reason,
                indexed_source_revision=task.last_indexed_source_revision,
            )
            item_results.append(
                ReconciliationItemResult(
                    item_id=metadata.item_id,
                    group_id=metadata.group_id,
                    external_task_key=metadata.external_task_key,
                    action=action,
                    reason=decision.reason,
                    refresh_reason=decision.reason,
                )
            )
        except Exception as exc:
            db.rollback()
            errors += 1
            logger.exception("Completed-transition reconciliation failed for task %s", task.external_task_key)
            log_refresh_decision(
                external_task_key=task.external_task_key, trigger_type="reconciliation",
                action="error", reason="check_failed",
            )
            if not dry_run:
                _record_check(
                    db, board_id=policy.board_id, item_id=task.item_id, scope="completed_transition",
                    outcome="error", reason=type(exc).__name__,
                )
                db.commit()
            item_results.append(
                ReconciliationItemResult(
                    item_id=task.item_id,
                    group_id=task.source_group_id,
                    external_task_key=task.external_task_key,
                    action="error",
                    reason=str(exc),
                    refresh_reason="check_failed",
                )
            )

    return ReconciliationResult(
        dry_run=dry_run,
        board_id=policy.board_id,
        scanned=len(tasks),
        skipped=skipped,
        completed_retained=completed_retained,
        errors=errors,
        source_unavailable=source_unavailable,
        items=tuple(item_results),
        **_coverage(db, board_id=policy.board_id, scope="completed_transition", item_ids=candidate_ids, dry_run=dry_run),
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
                page_size=args.page_size,
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
    parser.add_argument("--page-size", type=int, default=500, help="Monday API page size (1-500), independent of --limit")
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        active_result, completed_result = _run_from_new_session(args)
    except HTTPException as exc:
        logger.error("Auto-sync reconciliation command failed: %s", exc.detail)
        print(f"Auto-sync reconciliation failed: {exc.detail}")
        return 1
    except Exception as exc:
        logger.exception("Auto-sync reconciliation command failed unexpectedly")
        print(f"Auto-sync reconciliation failed unexpectedly: {exc}")
        return 1

    logger.info("Active auto-sync reconciliation result: %s", active_result)
    print(active_result)
    if completed_result is not None:
        logger.info("Completed-transition auto-sync reconciliation result: %s", completed_result)
        print(completed_result)
    return int(active_result.errors > 0 or (completed_result is not None and completed_result.errors > 0))


if __name__ == "__main__":
    raise SystemExit(main())
