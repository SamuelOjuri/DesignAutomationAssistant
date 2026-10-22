"""Run the reconciliation sweeps with one live group inventory per board."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..monday_client import list_items_in_groups
from .auto_sync import get_monday_ingestion_access_token
from .auto_sync_policy import policy_from_settings
from .auto_sync_reconciliation import reconcile_active_items_once, detect_completed_transitions_once
from .design_processing_policy import eligible_design_group_ids
from .design_processing_reconciliation import reconcile_landing_zone_once


logger = logging.getLogger(__name__)


def reconcile_monday_once(
    db: Session, *, dry_run: bool = True, access_token: Optional[str] = None,
    limit: int = 100, completed_limit: int = 100, design_limit: int = 100,
    page_size: int = 500, force: bool = False,
) -> dict:
    if min(limit, completed_limit, design_limit) < 1 or not 1 <= page_size <= 500:
        raise ValueError("Reconciliation limits must be positive and page_size must be between 1 and 500")
    design_enabled = settings.design_processing_mode != "off"
    if design_enabled and settings.design_processing_activation_timestamp is None:
        raise ValueError("broad design-processing reconciliation requires an activation timestamp")

    token = access_token or get_monday_ingestion_access_token()
    policy = policy_from_settings()
    groups_by_board = {policy.board_id: set(policy.active_group_ids)}
    design_board = str(settings.design_processing_board_id)
    if design_enabled:
        groups_by_board.setdefault(design_board, set()).update(eligible_design_group_ids(
            landing_group_id=str(settings.design_processing_landing_group_id),
        ))
    # Retain every page for fair selection. Discovery is shared only within this
    # invocation, so new items/moves never wait for an inventory cache to expire.
    inventory = {
        board_id: list_items_in_groups(token, board_id, sorted(groups), limit=page_size)
        for board_id, groups in groups_by_board.items()
    }
    active = reconcile_active_items_once(
        db, dry_run=dry_run, access_token=token, policy=policy, limit=limit,
        page_size=page_size, force=force, discovered_items=inventory[policy.board_id],
    )
    completed = detect_completed_transitions_once(
        db, dry_run=dry_run, access_token=token, policy=policy,
        limit=completed_limit, force=force,
    )
    design = reconcile_landing_zone_once(
        db, dry_run=dry_run, access_token=token, limit=design_limit,
        discovered_items=inventory.get(design_board),
    )
    return {"active": active, "completed": completed, "design": design}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--completed-limit", type=int, default=100)
    parser.add_argument("--design-limit", type=int, default=100)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--force", action="store_true", help="Bypass auto-sync freshness intervals")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    db = SessionLocal()
    try:
        results = reconcile_monday_once(db, **vars(args))
        print(json.dumps({name: asdict(result) for name, result in results.items()}, default=str))
        return int(any(result.errors for result in results.values()))
    except Exception:
        logger.exception("Combined Monday reconciliation failed")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
