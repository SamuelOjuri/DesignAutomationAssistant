from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import DesignProcessingItem, DesignProcessingReconciliationCheck
from ..monday_client import MondayGroupItem, MondayItemUnavailable, list_items_in_groups
from .auto_sync import get_monday_ingestion_access_token, utc_now
from .design_processing_observability import log_design_processing_event
from .design_processing_queue import queue_design_processing_snapshot
from .design_processing_policy import design_scope_exclusion, eligible_design_group_ids
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
    group_id: Optional[str] = None
    next_check_at: Optional[datetime] = None


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
    unavailable: int = 0
    deferred: int = 0
    items: tuple[DesignProcessingReconciliationItemResult, ...] = field(
        default_factory=tuple
    )


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("activation timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    # SQLite drops timezone metadata; PostgreSQL returns aware timestamps.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _record_check(
    db: Session, *, board_id: str, item_id: str, outcome: str, reason: str,
    now: datetime, next_check_at: Optional[datetime] = None,
    group_id: Optional[str] = None,
) -> None:
    values = dict(
        board_id=board_id, item_id=item_id, last_attempted_at=now,
        last_outcome=outcome, last_reason=reason, next_check_at=next_check_at,
    )
    if outcome not in {"error", "unavailable"}:
        values.update(last_checked_at=now, last_group_id=group_id)
    insert = postgres_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    statement = insert(DesignProcessingReconciliationCheck).values(**values)
    db.execute(statement.on_conflict_do_update(
        index_elements=["board_id", "item_id"],
        set_={key: value for key, value in values.items() if key not in {"board_id", "item_id"}},
    ))


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
) -> tuple[list[MondayGroupItem], list[DesignProcessingReconciliationItemResult]]:
    group_ids = sorted(eligible_design_group_ids(landing_group_id=group_id))
    items_by_group = list_items_in_groups(
        token,
        board_id,
        group_ids,
    )
    candidates: list[MondayGroupItem] = []
    skipped: list[DesignProcessingReconciliationItemResult] = []
    seen: set[str] = set()
    for eligible_group_id in group_ids:
        for summary in items_by_group.get(eligible_group_id, []):
            # An item can move between groups while their pages are fetched.
            if summary.item_id in seen:
                continue
            seen.add(summary.item_id)
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
    deferred_results: list[DesignProcessingReconciliationItemResult] = []
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
        )
        # New admission in Landing Zone and active groups is activation-bounded.
        # Previously admitted unfinished items are also checked outside those
        # groups, including older cancelled jobs and moves to Completed Folder.
        tracked = db.query(DesignProcessingItem).filter(
            DesignProcessingItem.board_id == board_id,
        ).all()
        tracked_by_id = {item.item_id: item for item in tracked}
        checks = {
            check.item_id: check
            for check in db.query(DesignProcessingReconciliationCheck).populate_existing().filter_by(
                board_id=board_id,
            ).all()
        }
        candidates_by_id = {candidate.item_id: candidate for candidate in candidates}
        for item in tracked:
            check = checks.get(item.item_id)
            if item.state != "ready_for_review" or (
                check is not None and check.last_outcome == "unavailable"
            ):
                candidates_by_id.setdefault(item.item_id, MondayGroupItem(item.item_id, None))
        # An unavailable lookup is not registration. Unregistered items must
        # still pass activation-bounded eligible-group admission (or an explicit
        # item-scoped command), even if an earlier lookup left a check record.

        def last_visit(candidate):
            check = checks.get(candidate.item_id)
            if check is not None:
                return (_stored_utc(check.last_attempted_at), candidate.item_id)
            stored = tracked_by_id.get(candidate.item_id)
            timestamp = stored.updated_at if stored is not None else _parse_created_at(
                candidate.created_at, item_id=candidate.item_id,
            )
            return (_stored_utc(timestamp), candidate.item_id)

        due_candidates = []
        for candidate in candidates_by_id.values():
            check = checks.get(candidate.item_id)
            # The old registration requirement no longer excludes active items.
            # Recheck rediscovered candidates now, while preserving all other
            # delays and the activation boundary for unregistered items.
            obsolete_admission_exclusion = (
                check is not None
                and check.last_outcome == "excluded"
                and check.last_reason == "not_registered_in_landing_zone"
            )
            if check is not None and check.next_check_at is not None and (
                _stored_utc(check.next_check_at) > reconciliation_now
            ) and not obsolete_admission_exclusion:
                deferred_results.append(DesignProcessingReconciliationItemResult(
                    item_id=candidate.item_id, action="deferred", reason=check.last_reason,
                    group_id=check.last_group_id, next_check_at=_stored_utc(check.next_check_at),
                ))
            else:
                due_candidates.append(candidate)
        candidates = sorted(due_candidates, key=last_visit)
        if limit is not None:
            candidates = candidates[:limit]
        selected = {candidate.item_id for candidate in candidates} | {
            result.item_id for result in deferred_results
        }
        prefiltered_results = [result for result in prefiltered_results if result.item_id not in selected]

    results = [*prefiltered_results, *deferred_results]
    queued = 0
    coalesced = 0
    skipped = len(prefiltered_results)
    excluded = 0
    errors = 0
    unavailable = 0

    for summary in candidates:
        try:
            try:
                snapshot = read_gateway.fetch_target(summary.item_id)
            except MondayItemUnavailable:
                # Only a validated empty lookup is an availability observation.
                # Authentication, malformed responses, throttling and other API
                # errors still take the error path and fail the cron run.
                checked_at = now or utc_now()
                next_check_at = checked_at + timedelta(
                    seconds=settings.design_processing_unavailable_recheck_seconds,
                )
                if not dry_run:
                    _record_check(
                        db, board_id=board_id, item_id=summary.item_id,
                        outcome="unavailable", reason="item_unavailable_to_worker",
                        now=checked_at, next_check_at=next_check_at,
                    )
                    db.commit()
                unavailable += 1
                results.append(DesignProcessingReconciliationItemResult(
                    item_id=summary.item_id,
                    action="would_unavailable" if dry_run else "unavailable",
                    reason="item_unavailable_to_worker", next_check_at=next_check_at,
                ))
                log_design_processing_event(
                    logger, "reconciliation_item_unavailable", level=logging.WARNING,
                    board_id=board_id, item_id=summary.item_id, dry_run=dry_run,
                    reason="item_unavailable_to_worker", next_check_at=next_check_at,
                )
                continue
            checked_at = now or utc_now()
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
                    now=checked_at,
                )
                if queue_result.item is not None:
                    queue_result.item.updated_at = checked_at
                db.flush()
                job_id = (
                    str(queue_result.job.id)
                    if queue_result.job is not None
                    else None
                )
                outcome = queue_result.outcome
                reason = queue_result.readiness or queue_result.outcome
                next_check_at = None
                if outcome == "excluded":
                    reason = design_scope_exclusion(
                        snapshot, expected_board_id=board_id, landing_group_id=group_id,
                    ) or reason
                    next_check_at = checked_at + timedelta(
                        seconds=settings.design_processing_excluded_recheck_seconds,
                    )
                if not dry_run:
                    _record_check(
                        db, board_id=board_id, item_id=summary.item_id,
                        outcome=outcome, reason=reason, now=checked_at,
                        next_check_at=next_check_at, group_id=snapshot.group_id,
                    )
                    db.commit()
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
                    group_id=snapshot.group_id,
                    next_check_at=next_check_at,
                )
            )
        except Exception as exc:
            db.rollback()
            errors += 1
            logger.exception(
                "Design-processing reconciliation failed for item %s",
                summary.item_id,
            )
            if not dry_run:
                try:
                    _record_check(
                        db, board_id=board_id, item_id=summary.item_id,
                        outcome="error", reason=str(exc)[:2000], now=now or utc_now(),
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("Failed to persist reconciliation error for item %s", summary.item_id)
            results.append(
                DesignProcessingReconciliationItemResult(
                    item_id=summary.item_id,
                    action="error",
                    reason=str(exc),
                )
            )

    result = DesignProcessingReconciliationResult(
        dry_run=dry_run,
        board_id=board_id,
        mode=configured_mode,
        scanned=len(candidates),
        queued=queued,
        coalesced=coalesced,
        skipped=skipped,
        excluded=excluded,
        errors=errors,
        unavailable=unavailable,
        deferred=len(deferred_results),
        items=tuple(results),
    )
    log_design_processing_event(
        logger,
        "reconciliation_completed",
        board_id=board_id,
        mode=configured_mode,
        dry_run=dry_run,
        scanned=result.scanned,
        queued=result.queued,
        coalesced=result.coalesced,
        skipped=result.skipped,
        excluded=result.excluded,
        errors=result.errors,
        unavailable=result.unavailable,
        deferred=result.deferred,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile Landing Zone and active-group admission and unfinished items"
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

    summary = asdict(result)
    summary.pop("items")
    summary["outcomes"] = dict(Counter(f"{item.action}:{item.reason}" for item in result.items))
    if args.item_id is not None:
        summary["items"] = [asdict(item) for item in result.items]
    print(json.dumps(summary, sort_keys=True, default=str))
    return 0 if result.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
