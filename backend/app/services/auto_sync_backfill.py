from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import logging
from typing import Optional

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Task
from ..monday_client import fetch_current_account_id, fetch_current_source_revision_inputs, list_item_ids_in_groups
from .auto_sync import (
    apply_auto_sync_policy_for_item,
    compute_desired_source_revision,
    get_monday_ingestion_access_token,
    item_metadata_from_monday_item,
    utc_now,
)
from .auto_sync_policy import AutoSyncPolicy, build_external_task_key, policy_from_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackfillItemResult:
    item_id: str
    group_id: Optional[str]
    external_task_key: Optional[str]
    action: str
    reason: str
    desired_source_revision: Optional[str] = None
    job_id: Optional[str] = None


@dataclass(frozen=True)
class ActiveBackfillResult:
    dry_run: bool
    board_id: str
    active_group_ids: tuple[str, ...]
    scanned: int = 0
    queued: int = 0
    skipped: int = 0
    errors: int = 0
    items: tuple[BackfillItemResult, ...] = field(default_factory=tuple)


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


def _dry_run_result(
    db: Session,
    item: dict,
    *,
    policy: AutoSyncPolicy,
    desired_source_revision: str,
    account_id: str,
) -> BackfillItemResult:
    metadata = item_metadata_from_monday_item(item, fallback_account_id=account_id)
    decision = policy.classify_group(metadata.board_id, metadata.group_id)
    external_task_key = build_external_task_key(metadata.account_id, metadata.board_id, metadata.item_id)

    existing_task = db.get(Task, external_task_key)
    if not decision.should_track_task:
        action = "ignored"
    elif not decision.should_queue_sync:
        action = "skipped"
    elif existing_task is not None and existing_task.last_indexed_source_revision == desired_source_revision:
        action = "fresh"
    else:
        action = "would_queue"

    return BackfillItemResult(
        item_id=metadata.item_id,
        group_id=metadata.group_id,
        external_task_key=external_task_key,
        action=action,
        reason=decision.reason,
        desired_source_revision=desired_source_revision,
    )


def active_backfill_once(
    db: Session,
    *,
    dry_run: bool = True,
    access_token: Optional[str] = None,
    policy: Optional[AutoSyncPolicy] = None,
    limit: Optional[int] = None,
) -> ActiveBackfillResult:
    policy = policy or policy_from_settings()
    token = access_token or get_monday_ingestion_access_token()
    account_id = fetch_current_account_id(token)
    batch_limit = limit or policy.backfill_batch_size
    group_ids = _ordered_active_group_ids(policy)
    item_ids_by_group = list_item_ids_in_groups(token, policy.board_id, group_ids, limit=max(batch_limit, 1))
    selected_items = _limited_item_ids_by_group(item_ids_by_group, group_ids, batch_limit)

    now = utc_now()
    item_results: list[BackfillItemResult] = []
    queued = 0
    skipped = 0
    errors = 0

    for expected_group_id, item_id in selected_items:
        try:
            item = fetch_current_source_revision_inputs(token, item_id, account_id=account_id)
            desired_source_revision = compute_desired_source_revision(item)
            if dry_run:
                item_result = _dry_run_result(
                    db,
                    item,
                    policy=policy,
                    desired_source_revision=desired_source_revision,
                    account_id=account_id,
                )
                if item_result.action == "would_queue":
                    queued += 1
                else:
                    skipped += 1
                item_results.append(item_result)
                continue

            queue_result = apply_auto_sync_policy_for_item(
                db,
                item,
                trigger_type="backfill",
                desired_source_revision=desired_source_revision,
                policy=policy,
                now=now,
                schedule_immediately=True,
                fallback_account_id=account_id,
            )
            if queue_result.job is not None:
                queued += 1
                action = "queued"
                db.flush()
                job_id = str(queue_result.job.id) if queue_result.job.id else None
            else:
                skipped += 1
                action = "skipped"
                job_id = None

            metadata = item_metadata_from_monday_item(item, fallback_account_id=account_id)
            item_results.append(
                BackfillItemResult(
                    item_id=metadata.item_id,
                    group_id=metadata.group_id or expected_group_id,
                    external_task_key=metadata.external_task_key if queue_result.task is not None else None,
                    action=action,
                    reason=queue_result.decision.reason,
                    desired_source_revision=desired_source_revision,
                    job_id=job_id,
                )
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            errors += 1
            logger.exception("Active auto-sync backfill failed for item %s", item_id)
            item_results.append(
                BackfillItemResult(
                    item_id=str(item_id),
                    group_id=expected_group_id,
                    external_task_key=None,
                    action="error",
                    reason=str(exc),
                )
            )

    if dry_run:
        db.rollback()

    return ActiveBackfillResult(
        dry_run=dry_run,
        board_id=policy.board_id,
        active_group_ids=tuple(group_ids),
        scanned=len(selected_items),
        queued=queued,
        skipped=skipped,
        errors=errors,
        items=tuple(item_results),
    )


def _run_from_new_session(args: argparse.Namespace) -> ActiveBackfillResult:
    db = SessionLocal()
    try:
        return active_backfill_once(db, dry_run=args.dry_run, limit=args.limit)
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue active monday items for durable auto-sync")
    parser.add_argument("--limit", type=int, default=None, help="Maximum active items to inspect")
    parser.add_argument("--dry-run", action="store_true", help="Inspect active items without creating jobs")
    args = parser.parse_args()

    result = _run_from_new_session(args)
    logger.info("Active auto-sync backfill result: %s", result)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())