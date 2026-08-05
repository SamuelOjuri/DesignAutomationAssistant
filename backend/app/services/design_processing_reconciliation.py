from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..monday_client import MondayGroupItem, list_items_in_groups
from .auto_sync import get_monday_ingestion_access_token, utc_now
from .design_processing_queue import queue_design_processing_snapshot
from .design_processing_target import (
    DesignProcessingReadGateway,
    MondayDesignProcessingReadGateway,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DesignProcessingReconciliationItemResult:
    item_id: str
    action: str
    reason: str
    job_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class DesignProcessingReconciliationResult:
    dry_run: bool
    board_id: str
    mode: str
    scanned: int = 0
    queued: int = 0
    coalesced: int = 0
    skipped: int = 0
    excluded: int = 0
    errors: int = 0
    items: tuple[DesignProcessingReconciliationItemResult, ...] = field(
        default_factory=tuple
    )


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("activation timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _parse_created_at(value: Optional[str], *, item_id: str) -> datetime:
    if not value:
        raise ValueError(f"Monday item {item_id} is missing created_at")
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"Monday item {item_id} has an invalid created_at timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"Monday item {item_id} created_at must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _broad_reconciliation_candidates(
    token: str,
    *,
    board_id: str,
    group_id: str,
    activation_timestamp: datetime,
    limit: Optional[int],
) -> tuple[list[MondayGroupItem], list[DesignProcessingReconciliationItemResult]]:
    items_by_group = list_items_in_groups(
        token,
        board_id,
        [group_id],
    )
    candidates: list[MondayGroupItem] = []
    skipped: list[DesignProcessingReconciliationItemResult] = []
    for summary in items_by_group.get(group_id, []):
        created_at = _parse_created_at(summary.created_at, item_id=summary.item_id)
        if created_at < activation_timestamp:
            skipped.append(
                DesignProcessingReconciliationItemResult(
                    item_id=summary.item_id,
                    action="skipped",
                    reason="before_activation_timestamp",
                )
            )
            continue
        candidates.append(summary)
        if limit is not None and len(candidates) >= limit:
            break
    return candidates, skipped


def reconcile_landing_zone_once(
    db: Session,
    *,
    dry_run: bool = True,
    access_token: Optional[str] = None,
    gateway: Optional[DesignProcessingReadGateway] = None,
    mode: Optional[str] = None,
    activation_timestamp: Optional[datetime] = None,
    item_id: Optional[str] = None,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
) -> DesignProcessingReconciliationResult:
    configured_mode = mode or settings.design_processing_mode
    board_id = str(settings.design_processing_board_id)
    group_id = str(settings.design_processing_landing_group_id)
    if configured_mode == "off":
        return DesignProcessingReconciliationResult(
            dry_run=dry_run,
            board_id=board_id,
            mode=configured_mode,
        )
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")

    token = access_token or get_monday_ingestion_access_token()
    read_gateway = gateway or MondayDesignProcessingReadGateway(
        access_token=token,
        project_board_id=str(settings.design_processing_project_board_id),
    )
    reconciliation_now = now or utc_now()

    prefiltered_results: list[DesignProcessingReconciliationItemResult] = []
    if item_id is not None:
        candidates = [MondayGroupItem(item_id=str(item_id), created_at=None)]
    else:
        boundary = activation_timestamp or settings.design_processing_activation_timestamp
        if boundary is None:
            raise ValueError(
                "broad design-processing reconciliation requires an activation timestamp"
            )
        candidates, prefiltered_results = _broad_reconciliation_candidates(
            token,
            board_id=board_id,
            group_id=group_id,
            activation_timestamp=_as_aware_utc(boundary),
            limit=limit,
        )

    results = list(prefiltered_results)
    queued = 0
    coalesced = 0
    skipped = len(prefiltered_results)
    excluded = 0
    errors = 0

    for summary in candidates:
        try:
            snapshot = read_gateway.fetch_target(summary.item_id)
            savepoint = db.begin_nested() if dry_run else None
            try:
                queue_result = queue_design_processing_snapshot(
                    db,
                    snapshot,
                    trigger_type="reconciliation",
                    mode=configured_mode,
                    pipeline_version=settings.design_processing_pipeline_version,
                    expected_board_id=board_id,
                    expected_group_id=group_id,
                    allowlist_item_ids=settings.design_processing_allowlist_item_ids,
                    now=reconciliation_now,
                )
                db.flush()
                job_id = (
                    str(queue_result.job.id)
                    if queue_result.job is not None
                    else None
                )
                outcome = queue_result.outcome
                reason = queue_result.readiness or queue_result.outcome
            finally:
                if savepoint is not None:
                    savepoint.rollback()
                    db.expire_all()

            action = f"would_{outcome}" if dry_run else outcome
            if outcome == "queued":
                queued += 1
            elif outcome == "coalesced":
                coalesced += 1
            elif outcome == "excluded":
                excluded += 1
            else:
                skipped += 1
            results.append(
                DesignProcessingReconciliationItemResult(
                    item_id=summary.item_id,
                    action=action,
                    reason=reason,
                    job_id=job_id,
                )
            )
            if not dry_run:
                db.commit()
        except Exception as exc:
            db.rollback()
            errors += 1
            logger.exception(
                "Design-processing reconciliation failed for item %s",
                summary.item_id,
            )
            results.append(
                DesignProcessingReconciliationItemResult(
                    item_id=summary.item_id,
                    action="error",
                    reason=str(exc),
                )
            )

    return DesignProcessingReconciliationResult(
        dry_run=dry_run,
        board_id=board_id,
        mode=configured_mode,
        scanned=len(candidates),
        queued=queued,
        coalesced=coalesced,
        skipped=skipped,
        excluded=excluded,
        errors=errors,
        items=tuple(results),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile Landing Zone items with design-processing jobs"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--item-id", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = reconcile_landing_zone_once(
            db,
            dry_run=args.dry_run,
            item_id=args.item_id,
            limit=args.limit,
        )
    except (HTTPException, ValueError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        logger.error("Design-processing reconciliation failed: %s", detail)
        print(f"Design-processing reconciliation failed: {detail}")
        return 1
    except Exception as exc:
        logger.exception("Design-processing reconciliation failed unexpectedly")
        print(f"Design-processing reconciliation failed unexpectedly: {exc}")
        return 1
    finally:
        db.close()

    logger.info("Design-processing reconciliation result: %s", result)
    print(result)
    return 0 if result.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
