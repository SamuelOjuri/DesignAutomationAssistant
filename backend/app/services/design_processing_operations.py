from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
import os
from typing import Any, Optional
import uuid

from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import (
    DESIGN_PROCESSING_ACTIVE_JOB_STATUSES,
    DesignProcessingItem,
    DesignProcessingJob,
)
from .auto_sync import get_monday_ingestion_access_token, utc_now
from .design_processing_observability import (
    collect_design_processing_metrics,
    log_design_processing_event,
)
from .design_processing_pipeline import cleanup_delete_pending_artifacts
from .design_processing_queue import (
    DesignProcessingMode,
    execution_allowed_for_item,
    queue_design_processing_snapshot,
)
from .design_processing_reconciliation import reconcile_landing_zone_once
from .design_processing_state import desired_identity, execution_identity
from .design_processing_target import (
    DesignProcessingReadGateway,
    MondayDesignProcessingReadGateway,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FailedJobRetryResult:
    job_id: str
    item_id: str
    outcome: str
    status: str


def _with_row_lock(db: Session, query):
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        return query.with_for_update()
    return query


def enqueue_design_processing_item(
    db: Session,
    item_id: str,
    *,
    gateway: DesignProcessingReadGateway,
    mode: DesignProcessingMode,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    normalized_item_id = str(item_id).strip()
    if not normalized_item_id:
        raise ValueError("item_id must not be empty")
    if mode == "off":
        return {
            "itemId": normalized_item_id,
            "outcome": "disabled",
            "readiness": None,
            "jobId": None,
            "createdJob": False,
        }
    snapshot = gateway.fetch_target(normalized_item_id)
    result = queue_design_processing_snapshot(
        db,
        snapshot,
        trigger_type="operator_enqueue",
        mode=mode,
        pipeline_version=settings.design_processing_pipeline_version,
        expected_board_id=str(settings.design_processing_board_id),
        expected_group_id=str(settings.design_processing_landing_group_id),
        allowlist_item_ids=settings.design_processing_allowlist_item_ids,
        now=now or utc_now(),
    )
    db.commit()
    return {
        "itemId": normalized_item_id,
        "outcome": result.outcome,
        "readiness": result.readiness,
        "jobId": str(result.job.id) if result.job is not None else None,
        "createdJob": result.created_job,
    }


def retry_failed_design_processing_job(
    db: Session,
    job_id: object,
    *,
    mode: DesignProcessingMode,
    allowlist_item_ids: tuple[str, ...] | list[str] = (),
    now: Optional[datetime] = None,
) -> FailedJobRetryResult:
    retry_at = now or utc_now()
    job = _with_row_lock(
        db,
        db.query(DesignProcessingJob).filter(DesignProcessingJob.id == job_id),
    ).one_or_none()
    if job is None:
        raise ValueError(f"design-processing job {job_id} was not found")
    item = _with_row_lock(
        db,
        db.query(DesignProcessingItem).filter(
            DesignProcessingItem.board_id == job.board_id,
            DesignProcessingItem.item_id == job.item_id,
        ),
    ).one()

    active = db.query(DesignProcessingJob).filter(
        DesignProcessingJob.board_id == job.board_id,
        DesignProcessingJob.item_id == job.item_id,
        DesignProcessingJob.status.in_(DESIGN_PROCESSING_ACTIVE_JOB_STATUSES),
    ).first()
    if active is not None:
        db.rollback()
        return FailedJobRetryResult(
            job_id=str(active.id),
            item_id=job.item_id,
            outcome="coalesced",
            status=active.status,
        )
    if job.status != "failed":
        raise ValueError(f"job {job.id} is {job.status!r}, not 'failed'")

    execution = execution_identity(job)
    execution_kind = job.execution_kind or "analysis"
    if execution is not None and execution != desired_identity(item):
        raise ValueError("failed job execution identity is no longer desired")
    if not execution_allowed_for_item(
        mode=mode,
        execution_kind=execution_kind,
        item_id=item.item_id,
        allowlist_item_ids=allowlist_item_ids,
    ):
        raise ValueError("current operational policy does not permit this job retry")

    job.status = "scheduled"
    job.trigger_type = "operator_retry"
    job.scheduled_for = retry_at
    job.next_retry_at = None
    job.locked_at = None
    job.locked_by = None
    job.heartbeat_at = None
    job.completed_at = None
    job.max_attempts = max(job.max_attempts, job.attempt_count + 1)
    job.updated_at = retry_at
    item.state = "analyzed" if execution_kind == "publication" else "scheduled"
    item.updated_at = retry_at
    db.commit()
    return FailedJobRetryResult(
        job_id=str(job.id),
        item_id=job.item_id,
        outcome="scheduled",
        status=job.status,
    )


def retry_design_processing_cleanup(
    db: Session,
    *,
    gateway: DesignProcessingReadGateway,
    item_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    deleted, failed = cleanup_delete_pending_artifacts(
        db,
        gateway=gateway,
        item_id=item_id,
        limit=limit,
        cleanup_policy=lambda candidate_item_id: execution_allowed_for_item(
            mode=settings.design_processing_mode,
            execution_kind="publication",
            item_id=candidate_item_id,
            allowlist_item_ids=settings.design_processing_allowlist_item_ids,
        ),
    )
    return {
        "itemId": str(item_id) if item_id is not None else None,
        "deleted": deleted,
        "failed": failed,
    }


def _gateway() -> MondayDesignProcessingReadGateway:
    return MondayDesignProcessingReadGateway(
        access_token=get_monday_ingestion_access_token(),
        project_board_id=str(settings.design_processing_project_board_id),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Operate and inspect durable design processing",
    )
    parser.add_argument(
        "--operator-id",
        default=os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue-item")
    enqueue.add_argument("--item-id", required=True)

    reconcile_item = subparsers.add_parser("reconcile-item")
    reconcile_item.add_argument("--item-id", required=True)
    reconcile_item.add_argument("--dry-run", action="store_true")

    retry_job = subparsers.add_parser("retry-failed-job")
    retry_job.add_argument("--job-id", required=True, type=uuid.UUID)

    cleanup = subparsers.add_parser("retry-artifact-cleanup")
    cleanup.add_argument("--item-id")
    cleanup.add_argument("--limit", type=int)

    reconcile_mode = subparsers.add_parser("reconcile-mode-transition")
    reconcile_mode.add_argument("--dry-run", action="store_true")
    reconcile_mode.add_argument("--limit", type=int)

    metrics = subparsers.add_parser("metrics")
    metrics.add_argument("--lease-timeout-seconds", type=int, default=3600)
    return parser.parse_args()


def _run_command(db: Session, args: argparse.Namespace) -> Any:
    if args.command == "enqueue-item":
        return enqueue_design_processing_item(
            db,
            args.item_id,
            gateway=_gateway(),
            mode=settings.design_processing_mode,
        )
    if args.command == "reconcile-item":
        return reconcile_landing_zone_once(
            db,
            dry_run=args.dry_run,
            gateway=_gateway(),
            mode=settings.design_processing_mode,
            item_id=args.item_id,
        )
    if args.command == "retry-failed-job":
        return retry_failed_design_processing_job(
            db,
            args.job_id,
            mode=settings.design_processing_mode,
            allowlist_item_ids=settings.design_processing_allowlist_item_ids,
        )
    if args.command == "retry-artifact-cleanup":
        return retry_design_processing_cleanup(
            db,
            gateway=_gateway(),
            item_id=args.item_id,
            limit=args.limit,
        )
    if args.command == "reconcile-mode-transition":
        return reconcile_landing_zone_once(
            db,
            dry_run=args.dry_run,
            gateway=_gateway(),
            mode=settings.design_processing_mode,
            limit=args.limit,
        )
    if args.command == "metrics":
        return collect_design_processing_metrics(
            db,
            lease_timeout_seconds=args.lease_timeout_seconds,
        )
    raise ValueError(f"unknown command {args.command!r}")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value


def main() -> int:
    args = _parse_args()
    command_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        result = _jsonable(_run_command(db, args))
        payload = {
            "commandId": command_id,
            "operatorId": args.operator_id,
            "command": args.command,
            "status": "succeeded",
            "result": result,
        }
        log_design_processing_event(
            logger,
            "operator_command",
            **payload,
        )
        print(json.dumps(payload, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        db.rollback()
        payload = {
            "commandId": command_id,
            "operatorId": args.operator_id,
            "command": args.command,
            "status": "failed",
            "error": str(exc),
        }
        log_design_processing_event(
            logger,
            "operator_command",
            level=logging.ERROR,
            **payload,
        )
        print(json.dumps(payload, sort_keys=True))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())