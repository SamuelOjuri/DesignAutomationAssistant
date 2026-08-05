from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Literal, Optional
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    DESIGN_PROCESSING_ACTIVE_JOB_STATUSES,
    DesignProcessingItem,
    DesignProcessingJob,
)
from .design_processing_inputs import DesignProcessingTargetSnapshot
from .design_processing_state import (
    cancel_job,
    cancel_superseded_execution,
    desired_identity,
    execution_identity,
    next_obligation,
)
from .design_processing_target import (
    RefreshedDesignProcessingTarget,
    apply_current_target_snapshot,
)


DesignProcessingMode = Literal["off", "shadow", "allowlist", "enabled"]
QueueOutcome = Literal["queued", "coalesced", "excluded", "ignored", "disabled"]


@dataclass(frozen=True, slots=True)
class DesignProcessingQueueResult:
    item: Optional[DesignProcessingItem]
    job: Optional[DesignProcessingJob]
    outcome: QueueOutcome
    readiness: Optional[str]
    created_item: bool = False
    created_job: bool = False


def publication_allowed_for_item(
    *,
    mode: DesignProcessingMode,
    item_id: str,
    allowlist_item_ids: Iterable[str] = (),
) -> bool:
    if mode == "enabled":
        return True
    if mode == "allowlist":
        return str(item_id) in {str(value) for value in allowlist_item_ids}
    return False


def readiness_backoff_seconds(
    readiness_check_count: int,
    *,
    initial_interval_seconds: int,
    maximum_interval_seconds: int,
) -> int:
    if readiness_check_count < 0:
        raise ValueError("readiness_check_count must not be negative")
    if initial_interval_seconds <= 0 or maximum_interval_seconds <= 0:
        raise ValueError("readiness backoff intervals must be positive")
    if maximum_interval_seconds < initial_interval_seconds:
        raise ValueError("maximum readiness interval must not be below initial interval")

    exponent = min(readiness_check_count, 62)
    return min(
        maximum_interval_seconds,
        initial_interval_seconds * (2**exponent),
    )


def next_readiness_check_at(
    now: datetime,
    readiness_check_count: int,
    *,
    initial_interval_seconds: int,
    maximum_interval_seconds: int,
) -> datetime:
    return now + timedelta(
        seconds=readiness_backoff_seconds(
            readiness_check_count,
            initial_interval_seconds=initial_interval_seconds,
            maximum_interval_seconds=maximum_interval_seconds,
        )
    )


def _supports_row_locks(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def _item_query(db: Session, *, board_id: str, item_id: str):
    query = db.query(DesignProcessingItem).filter(
        DesignProcessingItem.board_id == str(board_id),
        DesignProcessingItem.item_id == str(item_id),
    )
    return query.with_for_update() if _supports_row_locks(db) else query


def _active_job_query(db: Session, *, board_id: str, item_id: str):
    query = (
        db.query(DesignProcessingJob)
        .filter(
            DesignProcessingJob.board_id == str(board_id),
            DesignProcessingJob.item_id == str(item_id),
            DesignProcessingJob.status.in_(DESIGN_PROCESSING_ACTIVE_JOB_STATUSES),
        )
        .order_by(DesignProcessingJob.created_at.asc(), DesignProcessingJob.id.asc())
    )
    return query.with_for_update() if _supports_row_locks(db) else query


def upsert_design_processing_item(
    db: Session,
    *,
    board_id: str,
    item_id: str,
    initial_state: str,
    now: datetime,
) -> tuple[DesignProcessingItem, bool]:
    item = _item_query(db, board_id=board_id, item_id=item_id).one_or_none()
    if item is not None:
        return item, False

    candidate = DesignProcessingItem(
        id=uuid.uuid4(),
        board_id=str(board_id),
        item_id=str(item_id),
        state=initial_state,
        warnings_json=[],
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(candidate)
            db.flush([candidate])
        return candidate, True
    except IntegrityError:
        item = _item_query(db, board_id=board_id, item_id=item_id).one_or_none()
        if item is None:
            raise
        return item, False


def _new_job(
    item: DesignProcessingItem,
    *,
    trigger_type: str,
    stage: Optional[str],
    now: datetime,
) -> DesignProcessingJob:
    return DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=item.board_id,
        item_id=item.item_id,
        trigger_type=trigger_type,
        status="scheduled",
        stage=stage,
        scheduled_for=now,
        attempt_count=0,
        readiness_check_count=0,
        max_attempts=3,
        created_at=now,
        updated_at=now,
    )


def _create_job(
    db: Session,
    item: DesignProcessingItem,
    *,
    trigger_type: str,
    stage: Optional[str],
    now: datetime,
) -> tuple[DesignProcessingJob, bool]:
    candidate = _new_job(
        item,
        trigger_type=trigger_type,
        stage=stage,
        now=now,
    )
    try:
        with db.begin_nested():
            db.add(candidate)
            db.flush([candidate])
        return candidate, True
    except IntegrityError:
        existing = _active_job_query(
            db,
            board_id=item.board_id,
            item_id=item.item_id,
        ).first()
        if existing is None:
            raise
        return existing, False


def _wake_unclaimed_job(
    job: DesignProcessingJob,
    *,
    trigger_type: str,
    readiness_stage: Optional[str],
    now: datetime,
) -> None:
    if job.status == "running":
        return
    job.trigger_type = trigger_type
    job.status = "scheduled"
    job.scheduled_for = now
    job.next_retry_at = None
    job.locked_at = None
    job.locked_by = None
    job.heartbeat_at = None
    if execution_identity(job) is None:
        job.stage = readiness_stage
    job.updated_at = now


def cancel_and_schedule_successor(
    db: Session,
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    trigger_type: str,
    readiness_stage: Optional[str],
    now: datetime,
) -> DesignProcessingJob:
    if execution_identity(job) is None:
        raise ValueError("only an assigned execution can be superseded")
    cancel_superseded_execution(item, job, now=now)
    if readiness_stage is not None:
        item.state = readiness_stage
        item.updated_at = now
    db.flush([item, job])
    successor, _ = _create_job(
        db,
        item,
        trigger_type=trigger_type,
        stage=readiness_stage,
        now=now,
    )
    return successor


def _requires_job(
    item: DesignProcessingItem,
    refreshed: RefreshedDesignProcessingTarget,
    *,
    publication_allowed: bool,
) -> bool:
    if refreshed.readiness in {"waiting_for_name", "waiting_for_email"}:
        return True
    return next_obligation(item, publication_allowed=publication_allowed) is not None


def queue_design_processing_snapshot(
    db: Session,
    snapshot: DesignProcessingTargetSnapshot,
    *,
    trigger_type: str,
    mode: DesignProcessingMode,
    pipeline_version: str,
    expected_board_id: str,
    expected_group_id: str,
    allowlist_item_ids: Iterable[str] = (),
    now: datetime,
) -> DesignProcessingQueueResult:
    if mode == "off":
        return DesignProcessingQueueResult(
            item=None,
            job=None,
            outcome="disabled",
            readiness=None,
        )

    stored_item = _item_query(
        db,
        board_id=expected_board_id,
        item_id=snapshot.item_id,
    ).one_or_none()
    if (
        snapshot.board_id != str(expected_board_id)
        or snapshot.group_id != str(expected_group_id)
    ):
        if stored_item is None:
            return DesignProcessingQueueResult(
                item=None,
                job=None,
                outcome="excluded",
                readiness="ineligible",
            )
        active_job = _active_job_query(
            db,
            board_id=stored_item.board_id,
            item_id=stored_item.item_id,
        ).first()
        refreshed = apply_current_target_snapshot(
            stored_item,
            snapshot,
            pipeline_version=pipeline_version,
            expected_board_id=expected_board_id,
            expected_group_id=expected_group_id,
            now=now,
            active_job=active_job,
        )
        if active_job is not None:
            cancel_job(
                stored_item,
                active_job,
                reason="item is no longer in the design-processing Landing Zone",
                now=now,
                item_state="ineligible",
            )
        return DesignProcessingQueueResult(
            item=stored_item,
            job=None,
            outcome="excluded",
            readiness=refreshed.readiness,
        )

    item, created_item = upsert_design_processing_item(
        db,
        board_id=expected_board_id,
        item_id=snapshot.item_id,
        initial_state="scheduled",
        now=now,
    )
    active_job = _active_job_query(
        db,
        board_id=item.board_id,
        item_id=item.item_id,
    ).first()
    refreshed = apply_current_target_snapshot(
        item,
        snapshot,
        pipeline_version=pipeline_version,
        expected_board_id=expected_board_id,
        expected_group_id=expected_group_id,
        now=now,
        active_job=active_job,
    )
    publication_allowed = publication_allowed_for_item(
        mode=mode,
        item_id=item.item_id,
        allowlist_item_ids=allowlist_item_ids,
    )
    requires_job = _requires_job(
        item,
        refreshed,
        publication_allowed=publication_allowed,
    )
    readiness_stage = (
        refreshed.readiness if refreshed.readiness != "ready" else None
    )

    if active_job is not None and active_job.status == "running":
        return DesignProcessingQueueResult(
            item=item,
            job=active_job,
            outcome="coalesced",
            readiness=refreshed.readiness,
            created_item=created_item,
        )

    if active_job is not None and execution_identity(active_job) is not None:
        execution_is_stale = execution_identity(active_job) != desired_identity(item)
        publication_was_disabled = (
            active_job.execution_kind == "publication" and not publication_allowed
        )
        if execution_is_stale or publication_was_disabled:
            if requires_job:
                active_job = cancel_and_schedule_successor(
                    db,
                    item,
                    active_job,
                    trigger_type=trigger_type,
                    readiness_stage=readiness_stage,
                    now=now,
                )
            else:
                cancel_job(
                    item,
                    active_job,
                    reason="assigned execution is no longer required",
                    now=now,
                    item_state=(
                        refreshed.readiness
                        if refreshed.readiness != "ready"
                        else "scheduled"
                    ),
                )
                active_job = None

    if active_job is not None:
        _wake_unclaimed_job(
            active_job,
            trigger_type=trigger_type,
            readiness_stage=readiness_stage,
            now=now,
        )
        return DesignProcessingQueueResult(
            item=item,
            job=active_job,
            outcome="coalesced",
            readiness=refreshed.readiness,
            created_item=created_item,
        )

    if not requires_job:
        if (
            desired_identity(item) is not None
            and item.latest_analyzed_input_revision == item.latest_desired_input_revision
            and item.latest_analyzed_pipeline_version == item.latest_desired_pipeline_version
            and (
                item.latest_published_input_revision != item.latest_desired_input_revision
                or item.latest_published_pipeline_version
                != item.latest_desired_pipeline_version
            )
        ):
            item.state = "analyzed"
            item.updated_at = now
        return DesignProcessingQueueResult(
            item=item,
            job=None,
            outcome="ignored",
            readiness=refreshed.readiness,
            created_item=created_item,
        )

    job, created_job = _create_job(
        db,
        item,
        trigger_type=trigger_type,
        stage=readiness_stage,
        now=now,
    )
    return DesignProcessingQueueResult(
        item=item,
        job=job,
        outcome="queued" if created_job else "coalesced",
        readiness=refreshed.readiness,
        created_item=created_item,
        created_job=created_job,
    )
