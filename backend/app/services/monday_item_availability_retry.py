"""Preview or request a fresh processing check without bypassing item scope.

    python -m backend.app.services.monday_item_availability_retry --item-id 123
    python -m backend.app.services.monday_item_availability_retry --item-id 123 --apply

No remote calls. Confirmed retirement requires positive restoration evidence and
cannot be reset by this command. Existing pending work is made due; stored files,
decisions and failed model jobs are retained.
"""

import argparse
import json
import re

from ..config import settings
from ..db import SessionLocal
from ..models import (AutoSyncJob, DesignProcessingJob, DesignProcessingReconciliationCheck,
                      PerformanceSpecification, Task, TaskMondayMetadata, AutoSyncReconciliationCheck)
from .monday_item_availability import availability_decision, request_retry, utc_now


def retry_items(db, board_id, item_ids, *, apply=False, access_token=None):
    identities = list(dict.fromkeys(str(value) for value in item_ids))
    if not re.fullmatch(r"[1-9][0-9]*", str(board_id)) or any(
            not re.fullmatch(r"[1-9][0-9]*", identity) for identity in identities):
        raise ValueError("Board and item IDs must be positive decimal Monday IDs")
    results = []
    now = utc_now()
    for item_id in identities:
        decision = availability_decision(db, str(board_id), item_id, access_token)
        if decision.state == "retired":
            results.append({"item_id": item_id, "action": "refused", "reason": "confirmed_deleted"})
            continue
        if apply:
            if not request_retry(db, str(board_id), item_id, access_token):
                results.append({"item_id": item_id, "action": "refused", "reason": "confirmed_deleted"})
                continue
            # Reset reconciliation clocks as well as the common deadline. A
            # workflow still owns its admission and publication policy.
            checks = db.query(DesignProcessingReconciliationCheck).filter_by(board_id=str(board_id), item_id=item_id)
            for check in checks:
                check.next_check_at = now
            for check in db.query(AutoSyncReconciliationCheck).filter_by(board_id=str(board_id), item_id=item_id):
                if check.last_outcome != "retired":
                    check.last_checked_at = None
                    check.last_outcome, check.last_reason = "retry_requested", "operator_retry"
            for model in (AutoSyncJob, DesignProcessingJob):
                for job in db.query(model).filter(
                        model.board_id == str(board_id), model.item_id == item_id,
                        model.status.in_(("pending", "scheduled", "retry_wait"))):
                    job.scheduled_for = now
                    job.next_retry_at = now
            for record in db.query(PerformanceSpecification).filter_by(board_id=str(board_id), item_id=item_id):
                record.checked_at = None
                if record.scheduled_for is not None:
                    record.scheduled_for = now
            task_keys = db.query(Task.external_task_key).filter_by(board_id=str(board_id), item_id=item_id)
            for record in db.query(TaskMondayMetadata).filter(TaskMondayMetadata.external_task_key.in_(task_keys)):
                if record.requested_generation > record.completed_generation and record.lease_token is None:
                    record.scheduled_for = now
            db.commit()
        results.append({"item_id": item_id, "action": "retry_requested" if apply else "would_retry",
                        "previous_state": decision.state, "reason": decision.reason})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item-id", nargs="+", required=True)
    parser.add_argument("--board-id", default=str(settings.design_processing_board_id))
    parser.add_argument("--apply", action="store_true", help="Reset availability backoff and existing pending schedules")
    args = parser.parse_args()
    if not re.fullmatch(r"[1-9][0-9]*", args.board_id):
        parser.error("board-id must be a positive decimal Monday ID")
    with SessionLocal() as db:
        results = retry_items(db, args.board_id, args.item_id, apply=args.apply)
    print(json.dumps({"applied": args.apply, "items": results}, sort_keys=True))
    return int(any(row["action"] == "refused" for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
