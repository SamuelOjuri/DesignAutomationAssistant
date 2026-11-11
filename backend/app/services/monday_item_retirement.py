"""Retire tracked items whose confirmed Monday deletion is no longer observable.

Monday keeps deleted items in its trash only temporarily. A deletion missed during
that window leaves tasks active, and lifecycle checks keep receiving empty lookups.
After confirming the deletion (for example in the board activity log), this applies
the lifecycle a live delete event applies, cancels dependent work and records a
terminal reconciliation outcome so scheduled checks stop. Items Monday still
returns, including archived or trashed ones, are refused: reconciliation owns them.

    python -m backend.app.services.monday_item_retirement --item-id 123 456 --dry-run
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import logging
from typing import Any, Callable, Iterable, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import (
    DESIGN_PROCESSING_ACTIVE_JOB_STATUSES, DesignProcessingItem, DesignProcessingJob,
    DesignProcessingReconciliationCheck, Task,
)
from ..monday_client import MondayItemUnavailable, fetch_design_processing_intake_item
from .auto_sync import apply_auto_sync_policy_for_item, get_monday_ingestion_access_token, utc_now
from .auto_sync_policy import RETIRED_SOURCE_OUTCOME, RETIRED_SOURCE_REASON, policy_from_settings
from .auto_sync_reconciliation import _record_check as record_lifecycle_check
from .design_processing_reconciliation import _record_check as record_design_check
from .design_processing_state import cancel_job
from .performance_specification import cancel_specification
from .monday_item_availability import observe_item, record_retired

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ItemRetirementResult:
    item_id: str
    action: str
    reason: str
    tasks: int = 0
    design_state: Optional[str] = None
    monday_state: Optional[str] = None


def retire_deleted_items(
    db: Session,
    item_ids: Iterable[str],
    *,
    fetch_item: Callable[[str], dict[str, Any]],
    dry_run: bool = True,
    access_token: Optional[str] = None,
) -> list[ItemRetirementResult]:
    """Retire each item only if Monday returns no item at all, even inactive."""
    results = []
    for item_id in dict.fromkeys(str(value).strip() for value in item_ids):
        try:
            if not item_id.isdecimal():
                raise ValueError("item ID must be a positive decimal Monday ID")
            results.append(_retire_item(db, item_id, fetch_item=fetch_item, dry_run=dry_run, access_token=access_token))
        except Exception as exc:
            db.rollback()
            logger.exception("Monday item retirement failed item=%s", item_id)
            results.append(ItemRetirementResult(item_id, "error", f"{type(exc).__name__}: {exc}"))
    return results


def _retire_item(db, item_id, *, fetch_item, dry_run, access_token=None):
    policy = policy_from_settings()
    design_board_id = str(settings.design_processing_board_id)
    tasks = db.query(Task).filter_by(board_id=policy.board_id, item_id=item_id).order_by(Task.external_task_key).all()
    design_item = db.query(DesignProcessingItem).filter_by(board_id=design_board_id, item_id=item_id).one_or_none()
    if not tasks and design_item is None:
        return ItemRetirementResult(item_id, "not_tracked", "no_task")
    try:
        visible = fetch_item(item_id)
    except MondayItemUnavailable:
        visible = None
    if visible is not None:
        if not dry_run and str(visible.get("id")) == item_id and isinstance(visible.get("board"), dict):
            observe_item(db, design_board_id if design_item is not None else policy.board_id,
                visible, access_token=access_token)
            db.commit()
        return ItemRetirementResult(item_id, "visible_on_monday", "reconciliation_owns_visible_items",
                                    tasks=len(tasks), monday_state=visible.get("state"))
    design_state = design_item.state if design_item is not None else None
    if dry_run:
        return ItemRetirementResult(item_id, "would_retire", RETIRED_SOURCE_REASON,
                                    tasks=len(tasks), design_state=design_state)

    now = utc_now()
    # Shared availability is always locked before specification/job state.
    # This also fences a running probe before cancellation acquires other locks.
    for board_id in {*(task.board_id for task in tasks), *([design_board_id] if design_item is not None else [])}:
        record_retired(db, board_id, item_id, access_token=access_token, observed_at=now)
    for task in tasks:
        # The same lifecycle a live delete event applies, with the last stored group.
        result = apply_auto_sync_policy_for_item(
            db,
            {"id": item_id, "state": "deleted", "board": {"id": task.board_id},
             "group": {"id": task.source_group_id, "title": task.source_group_title}},
            trigger_type="operator_retire", policy=policy, now=now, fallback_account_id=task.account_id,
        )
        if result.task is None or result.task.auto_sync_state != "deleted":
            raise RuntimeError(f"task {task.external_task_key} was not marked deleted")
    cancel_specification(db, design_board_id, item_id, reason=RETIRED_SOURCE_REASON)
    if design_item is not None:
        jobs = db.query(DesignProcessingJob).filter(
            DesignProcessingJob.board_id == design_board_id, DesignProcessingJob.item_id == item_id,
            DesignProcessingJob.status.in_(DESIGN_PROCESSING_ACTIVE_JOB_STATUSES),
        ).all()
        for job in jobs:
            cancel_job(design_item, job, reason="item_deleted", now=now, item_state="ineligible")
        design_item.state, design_item.updated_at = "ineligible", now
    if tasks:
        record_lifecycle_check(db, board_id=policy.board_id, item_id=item_id, scope="completed_transition",
                               outcome=RETIRED_SOURCE_OUTCOME, reason=RETIRED_SOURCE_REASON)
    if design_item is not None or db.get(DesignProcessingReconciliationCheck, (design_board_id, item_id)) is not None:
        record_design_check(db, board_id=design_board_id, item_id=item_id,
                            outcome=RETIRED_SOURCE_OUTCOME, reason=RETIRED_SOURCE_REASON, now=now)
    db.commit()
    logger.info(
        "Retired confirmed-deleted Monday item=%s tasks=%s design_state=%s", item_id, len(tasks), design_state,
        extra={"event": "monday_item_retired", "board_id": policy.board_id if tasks else design_board_id, "item_id": item_id},
    )
    return ItemRetirementResult(item_id, "retired", RETIRED_SOURCE_REASON, tasks=len(tasks),
                                design_state="ineligible" if design_item is not None else None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Retire tracked items confirmed deleted and no longer returned by Monday")
    parser.add_argument("--item-id", dest="item_ids", action="extend", nargs="+", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Check Monday and report without changing data")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = get_monday_ingestion_access_token()
    db = SessionLocal()
    try:
        results = retire_deleted_items(
            db, args.item_ids, dry_run=args.dry_run, access_token=token,
            fetch_item=lambda item_id: fetch_design_processing_intake_item(token, item_id),
        )
    finally:
        db.close()
    print(json.dumps({"dry_run": args.dry_run, "items": [asdict(result) for result in results]}, sort_keys=True))
    expected = "would_retire" if args.dry_run else "retired"
    return 0 if all(result.action == expected for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
