from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import logging
import socket
import threading
import time
from typing import Iterable, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import DesignProcessingItem, DesignProcessingJob
from .auto_sync import get_monday_ingestion_access_token, utc_now
from .design_processing_artifacts import (
    DesignArtifactStorage,
    SupabaseDesignArtifactStorage,
)
from .design_processing_observability import log_design_processing_event
from .design_processing_pipeline import (
    AssetDownloader,
    ExecutionPolicy,
    run_analysis_pipeline,
    run_publication_pipeline,
)
from .design_processing_queue import (
    DesignProcessingMode,
    cancel_and_schedule_successor,
    execution_allowed_for_item,
    next_readiness_check_at,
    publication_allowed_for_item,
)
from .design_processing_state import (
    assign_next_execution,
    cancel_job,
    claim_job,
    desired_identity,
    execution_identity,
    fail_job,
    resume_execution,
    schedule_execution_retry,
    transition_to_readiness_wait,
)
from .design_processing_target import (
    DesignProcessingReadGateway,
    DesignProcessingTargetMismatch,
    MondayDesignProcessingReadGateway,
    refresh_current_target,
)
from .legacy_enquiry.llm import LegacyGeminiClient


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DesignProcessingWorkerResult:
    recovered: int = 0
    claimed: int = 0
    analyzed: int = 0
    published: int = 0
    readiness_wait: int = 0
    redundant: int = 0
    retry_wait: int = 0
    failed: int = 0
    cancelled: int = 0


def _log_worker_batch(
    result: DesignProcessingWorkerResult,
    *,
    worker_id: str,
    mode: DesignProcessingMode,
) -> None:
    log_design_processing_event(
        logger,
        "worker_batch",
        worker_id=worker_id,
        mode=mode,
        **asdict(result),
    )


def default_worker_id() -> str:
    return (
        f"{socket.gethostname()}:{settings.design_processing_board_id}:"
        f"{time.time_ns()}"
    )


def _supports_skip_locked(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def _with_job_locks(db: Session, query):
    return query.with_for_update(skip_locked=True) if _supports_skip_locked(db) else query


def _with_row_lock(db: Session, query):
    return query.with_for_update() if _supports_skip_locked(db) else query


def _analysis_or_readiness_filter():
    return or_(
        DesignProcessingJob.execution_kind == "analysis",
        and_(
            DesignProcessingJob.execution_kind.is_(None),
            or_(
                DesignProcessingItem.latest_desired_input_revision.is_(None),
                DesignProcessingItem.latest_analyzed_input_revision.is_(None),
                DesignProcessingItem.latest_analyzed_input_revision
                != DesignProcessingItem.latest_desired_input_revision,
                DesignProcessingItem.latest_analyzed_pipeline_version
                != DesignProcessingItem.latest_desired_pipeline_version,
            ),
        ),
    )


def _load_item_for_job(
    db: Session,
    job: DesignProcessingJob,
    *,
    for_update: bool = False,
) -> DesignProcessingItem:
    query = db.query(DesignProcessingItem).filter(
        DesignProcessingItem.board_id == job.board_id,
        DesignProcessingItem.item_id == job.item_id,
    )
    if for_update:
        query = _with_row_lock(db, query)
    return query.one()


def claim_due_analysis_jobs(
    db: Session,
    *,
    worker_id: str,
    limit: int = 1,
    now: Optional[datetime] = None,
) -> list[DesignProcessingJob]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    claimed_at = now or utc_now()
    query = (
        db.query(DesignProcessingJob)
        .join(
            DesignProcessingItem,
            and_(
                DesignProcessingItem.board_id == DesignProcessingJob.board_id,
                DesignProcessingItem.item_id == DesignProcessingJob.item_id,
            ),
        )
        .filter(
            or_(
                and_(
                    DesignProcessingJob.status == "scheduled",
                    DesignProcessingJob.scheduled_for <= claimed_at,
                ),
                and_(
                    DesignProcessingJob.status == "retry_wait",
                    or_(
                        DesignProcessingJob.next_retry_at.is_(None),
                        DesignProcessingJob.next_retry_at <= claimed_at,
                    ),
                ),
            ),
            _analysis_or_readiness_filter(),
        )
        .order_by(
            DesignProcessingJob.scheduled_for.asc(),
            DesignProcessingJob.created_at.asc(),
            DesignProcessingJob.id.asc(),
        )
        .limit(limit)
    )
    jobs = _with_job_locks(db, query).all()
    for job in jobs:
        claim_job(job, worker_id=worker_id, now=claimed_at)
    if jobs:
        db.commit()
    return jobs


def claim_due_design_processing_jobs(
    db: Session,
    *,
    worker_id: str,
    limit: int = 1,
    now: Optional[datetime] = None,
) -> list[DesignProcessingJob]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    claimed_at = now or utc_now()
    query = (
        db.query(DesignProcessingJob)
        .filter(
            or_(
                and_(
                    DesignProcessingJob.status == "scheduled",
                    DesignProcessingJob.scheduled_for <= claimed_at,
                ),
                and_(
                    DesignProcessingJob.status == "retry_wait",
                    or_(
                        DesignProcessingJob.next_retry_at.is_(None),
                        DesignProcessingJob.next_retry_at <= claimed_at,
                    ),
                ),
            ),
        )
        .order_by(
            DesignProcessingJob.scheduled_for.asc(),
            DesignProcessingJob.created_at.asc(),
            DesignProcessingJob.id.asc(),
        )
        .limit(limit)
    )
    jobs = _with_job_locks(db, query).all()
    for job in jobs:
        claim_job(job, worker_id=worker_id, now=claimed_at)
    if jobs:
        db.commit()
    return jobs


def heartbeat_design_processing_job(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    now: Optional[datetime] = None,
) -> bool:
    job = db.get(DesignProcessingJob, job_id)
    if job is None or job.status != "running" or job.locked_by != worker_id:
        return False
    heartbeat_at = now or utc_now()
    job.heartbeat_at = heartbeat_at
    job.updated_at = heartbeat_at
    db.commit()
    return True


def _heartbeat_until_stopped(
    job_id: object,
    *,
    worker_id: str,
    stop_event: threading.Event,
    interval_seconds: float,
) -> None:
    while not stop_event.wait(interval_seconds):
        heartbeat_db = SessionLocal()
        try:
            if not heartbeat_design_processing_job(
                heartbeat_db,
                job_id,
                worker_id=worker_id,
            ):
                return
        except Exception:
            heartbeat_db.rollback()
            logger.exception(
                "Unable to update heartbeat for design-processing job %s",
                job_id,
            )
        finally:
            heartbeat_db.close()


def _start_heartbeat(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    interval_seconds: float,
) -> tuple[Optional[threading.Event], Optional[threading.Thread]]:
    if not _supports_skip_locked(db) or interval_seconds <= 0:
        return None, None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_heartbeat_until_stopped,
        kwargs={
            "job_id": job_id,
            "worker_id": worker_id,
            "stop_event": stop_event,
            "interval_seconds": interval_seconds,
        },
        name=f"design-processing-heartbeat-{job_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _stop_heartbeat(
    stop_event: Optional[threading.Event],
    thread: Optional[threading.Thread],
) -> None:
    if stop_event is not None and thread is not None:
        stop_event.set()
        thread.join(timeout=5)


def _normal_retry_at(now: datetime, attempt_count: int) -> datetime:
    delay_seconds = min(3600, 60 * (2 ** max(attempt_count - 1, 0)))
    return now + timedelta(seconds=delay_seconds)


def _elapsed_seconds(now: datetime, started_at: datetime) -> float:
    normalized_start = (
        started_at.replace(tzinfo=timezone.utc)
        if started_at.tzinfo is None or started_at.utcoffset() is None
        else started_at.astimezone(timezone.utc)
    )
    normalized_now = (
        now.replace(tzinfo=timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None
        else now.astimezone(timezone.utc)
    )
    return (normalized_now - normalized_start).total_seconds()


def recover_expired_analysis_leases(
    db: Session,
    *,
    lease_timeout_seconds: int = 3600,
    limit: int = 100,
    mode: DesignProcessingMode = "enabled",
    allowlist_item_ids: Iterable[str] = (),
    now: Optional[datetime] = None,
) -> int:
    if lease_timeout_seconds < 1:
        raise ValueError("lease_timeout_seconds must be positive")
    recovered_at = now or utc_now()
    cutoff = recovered_at - timedelta(seconds=lease_timeout_seconds)
    query = (
        db.query(DesignProcessingJob)
        .join(
            DesignProcessingItem,
            and_(
                DesignProcessingItem.board_id == DesignProcessingJob.board_id,
                DesignProcessingItem.item_id == DesignProcessingJob.item_id,
            ),
        )
        .filter(
            DesignProcessingJob.status == "running",
            _analysis_or_readiness_filter(),
            or_(
                DesignProcessingJob.locked_at.is_(None),
                DesignProcessingJob.heartbeat_at <= cutoff,
                and_(
                    DesignProcessingJob.heartbeat_at.is_(None),
                    DesignProcessingJob.locked_at <= cutoff,
                ),
            ),
        )
        .order_by(DesignProcessingJob.locked_at.asc())
        .limit(limit)
    )
    jobs = _with_job_locks(db, query).all()
    for job in jobs:
        item = _load_item_for_job(db, job, for_update=True)
        identity = execution_identity(job)
        execution_kind = job.execution_kind or "analysis"
        execution_allowed = execution_allowed_for_item(
            mode=mode,
            execution_kind=execution_kind,
            item_id=item.item_id,
            allowlist_item_ids=allowlist_item_ids,
        )
        if not execution_allowed:
            cancel_job(
                item,
                job,
                reason="expired lease is disallowed by current operational policy",
                now=recovered_at,
                item_state=(
                    "analyzed"
                    if execution_kind == "publication"
                    else item.state
                    if item.state in {"waiting_for_name", "waiting_for_email"}
                    else "scheduled"
                ),
            )
        elif identity is not None and identity != desired_identity(item):
            readiness_stage = (
                item.state
                if item.state in {"waiting_for_name", "waiting_for_email"}
                else None
            )
            cancel_and_schedule_successor(
                db,
                item,
                job,
                trigger_type="expired_lease_superseded",
                readiness_stage=readiness_stage,
                now=recovered_at,
            )
        elif identity is None:
            job.status = "scheduled"
            job.scheduled_for = recovered_at
            job.next_retry_at = None
            job.locked_at = None
            job.locked_by = None
            job.heartbeat_at = None
            job.last_error = "Worker lease expired during readiness validation"
            job.updated_at = recovered_at
        elif job.attempt_count >= job.max_attempts:
            fail_job(item, job, error="Worker lease expired", now=recovered_at)
        else:
            schedule_execution_retry(
                item,
                job,
                scheduled_for=recovered_at,
                error="Worker lease expired",
                now=recovered_at,
            )
    if jobs:
        db.commit()
    return len(jobs)


def recover_expired_design_processing_leases(
    db: Session,
    *,
    lease_timeout_seconds: int = 3600,
    limit: int = 100,
    mode: DesignProcessingMode = "enabled",
    allowlist_item_ids: Iterable[str] = (),
    now: Optional[datetime] = None,
) -> int:
    if lease_timeout_seconds < 1:
        raise ValueError("lease_timeout_seconds must be positive")
    recovered_at = now or utc_now()
    cutoff = recovered_at - timedelta(seconds=lease_timeout_seconds)
    query = (
        db.query(DesignProcessingJob)
        .filter(
            DesignProcessingJob.status == "running",
            or_(
                DesignProcessingJob.locked_at.is_(None),
                DesignProcessingJob.heartbeat_at <= cutoff,
                and_(
                    DesignProcessingJob.heartbeat_at.is_(None),
                    DesignProcessingJob.locked_at <= cutoff,
                ),
            ),
        )
        .order_by(DesignProcessingJob.locked_at.asc())
        .limit(limit)
    )
    jobs = _with_job_locks(db, query).all()
    for job in jobs:
        item = _load_item_for_job(db, job, for_update=True)
        identity = execution_identity(job)
        execution_kind = job.execution_kind or "analysis"
        execution_allowed = execution_allowed_for_item(
            mode=mode,
            execution_kind=execution_kind,
            item_id=item.item_id,
            allowlist_item_ids=allowlist_item_ids,
        )
        if not execution_allowed:
            cancel_job(
                item,
                job,
                reason="expired lease is disallowed by current operational policy",
                now=recovered_at,
                item_state=(
                    "analyzed"
                    if execution_kind == "publication"
                    else item.state
                    if item.state in {"waiting_for_name", "waiting_for_email"}
                    else "scheduled"
                ),
            )
        elif identity is not None and identity != desired_identity(item):
            readiness_stage = (
                item.state
                if item.state in {"waiting_for_name", "waiting_for_email"}
                else None
            )
            cancel_and_schedule_successor(
                db,
                item,
                job,
                trigger_type="expired_lease_superseded",
                readiness_stage=readiness_stage,
                now=recovered_at,
            )
        elif identity is None:
            job.status = "scheduled"
            job.scheduled_for = recovered_at
            job.next_retry_at = None
            job.locked_at = None
            job.locked_by = None
            job.heartbeat_at = None
            job.last_error = "Worker lease expired during readiness validation"
            job.updated_at = recovered_at
        elif job.attempt_count >= job.max_attempts:
            fail_job(item, job, error="Worker lease expired", now=recovered_at)
        else:
            schedule_execution_retry(
                item,
                job,
                scheduled_for=recovered_at,
                error="Worker lease expired",
                now=recovered_at,
            )
    if jobs:
        db.commit()
    return len(jobs)


def _cancel_or_replace_mismatched_job(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    gateway: DesignProcessingReadGateway,
    mode: DesignProcessingMode,
    execution_policy: Optional[ExecutionPolicy],
    now: datetime,
) -> str:
    job = _with_row_lock(
        db,
        db.query(DesignProcessingJob).filter(DesignProcessingJob.id == job_id),
    ).one_or_none()
    if job is None:
        db.rollback()
        return "missing"
    if job.status != "running" or job.locked_by != worker_id:
        db.rollback()
        return "not_claimed"
    item = _load_item_for_job(db, job, for_update=True)
    refreshed = refresh_current_target(
        item,
        gateway=gateway,
        pipeline_version=settings.design_processing_pipeline_version,
        expected_board_id=str(settings.design_processing_board_id),
        expected_group_id=str(settings.design_processing_landing_group_id),
        now=now,
        active_job=job,
    )
    execution_kind = job.execution_kind or "analysis"
    execution_allowed = (
        execution_policy(execution_kind, item.item_id)
        if execution_policy is not None
        else execution_allowed_for_item(
            mode=mode,
            execution_kind=execution_kind,
            item_id=item.item_id,
            allowlist_item_ids=settings.design_processing_allowlist_item_ids,
        )
    )
    if not execution_allowed:
        cancel_job(
            item,
            job,
            reason="current operational policy no longer permits this execution",
            now=now,
            item_state=(
                "analyzed"
                if execution_kind == "publication" and refreshed.readiness == "ready"
                else refreshed.readiness
                if refreshed.readiness != "ready"
                else "scheduled"
            ),
        )
        db.commit()
        return "cancelled"
    if refreshed.readiness == "ineligible":
        cancel_job(
            item,
            job,
            reason="current Monday item is no longer eligible for design processing",
            now=now,
            item_state="ineligible",
        )
        db.commit()
        return "cancelled"

    readiness_stage = (
        refreshed.readiness if refreshed.readiness != "ready" else None
    )
    if execution_identity(job) is None:
        cancel_job(
            item,
            job,
            reason="unassigned execution target changed during validation",
            now=now,
            item_state=readiness_stage or "scheduled",
        )
    else:
        cancel_and_schedule_successor(
            db,
            item,
            job,
            trigger_type="execution_superseded",
            readiness_stage=readiness_stage,
            now=now,
        )
    db.commit()
    return "cancelled"


def _record_execution_failure(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    error: str,
    now: datetime,
) -> str:
    job = _with_row_lock(
        db,
        db.query(DesignProcessingJob).filter(DesignProcessingJob.id == job_id),
    ).one_or_none()
    if job is None:
        db.rollback()
        return "missing"
    if job.status != "running" or job.locked_by != worker_id:
        db.rollback()
        return "not_claimed"
    item = _load_item_for_job(db, job, for_update=True)
    if execution_identity(job) is None:
        job.attempt_count += 1
        job.last_error = error
        job.locked_at = None
        job.locked_by = None
        job.heartbeat_at = None
        job.updated_at = now
        if job.attempt_count >= job.max_attempts:
            job.status = "failed"
            job.completed_at = now
            item.state = "failed"
            item.updated_at = now
            outcome = "failed"
        else:
            job.status = "retry_wait"
            job.scheduled_for = _normal_retry_at(now, job.attempt_count)
            job.next_retry_at = job.scheduled_for
            item.state = "scheduled"
            item.updated_at = now
            outcome = "retry_wait"
    elif job.attempt_count >= job.max_attempts:
        fail_job(item, job, error=error, now=now)
        outcome = "failed"
    else:
        schedule_execution_retry(
            item,
            job,
            scheduled_for=_normal_retry_at(now, job.attempt_count),
            error=error,
            now=now,
        )
        outcome = "retry_wait"
    db.commit()
    return outcome


def execute_claimed_analysis_job(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    access_token: str,
    gateway: DesignProcessingReadGateway,
    analysis_client: LegacyGeminiClient,
    artifact_storage: DesignArtifactStorage,
    mode: DesignProcessingMode,
    execution_policy: Optional[ExecutionPolicy] = None,
    downloader: Optional[AssetDownloader] = None,
    now: Optional[datetime] = None,
    heartbeat_interval_seconds: float = 60.0,
) -> str:
    execution_now = now or utc_now()
    job = db.get(DesignProcessingJob, job_id)
    if job is None:
        return "missing"
    if job.status != "running" or job.locked_by != worker_id:
        return "not_claimed"
    item = _load_item_for_job(db, job)
    def policy_allows(execution_kind: str) -> bool:
        if execution_policy is not None:
            return execution_policy(execution_kind, item.item_id)
        return execution_allowed_for_item(
            mode=mode,
            execution_kind=execution_kind,
            item_id=item.item_id,
            allowlist_item_ids=settings.design_processing_allowlist_item_ids,
        )

    publication_allowed = policy_allows("publication")

    if not policy_allows("analysis"):
        cancel_job(
            item,
            job,
            reason="design processing is disabled",
            now=execution_now,
            item_state=(
                item.state
                if item.state in {"waiting_for_name", "waiting_for_email"}
                else "scheduled"
            ),
        )
        db.commit()
        return "cancelled"

    try:
        if execution_identity(job) is None:
            refreshed = refresh_current_target(
                item,
                gateway=gateway,
                pipeline_version=settings.design_processing_pipeline_version,
                expected_board_id=str(settings.design_processing_board_id),
                expected_group_id=str(settings.design_processing_landing_group_id),
                now=execution_now,
                active_job=job,
            )
            if refreshed.readiness == "ineligible":
                cancel_job(
                    item,
                    job,
                    reason="item is outside the design-processing Landing Zone",
                    now=execution_now,
                    item_state="ineligible",
                )
                db.commit()
                return "cancelled"
            if refreshed.readiness in {"waiting_for_name", "waiting_for_email"}:
                scheduled_for = next_readiness_check_at(
                    execution_now,
                    job.readiness_check_count,
                    initial_interval_seconds=(
                        settings.design_processing_readiness_initial_interval_seconds
                    ),
                    maximum_interval_seconds=(
                        settings.design_processing_readiness_max_interval_seconds
                    ),
                )
                transition_to_readiness_wait(
                    item,
                    job,
                    missing_name=refreshed.readiness == "waiting_for_name",
                    missing_email=refreshed.readiness == "waiting_for_email",
                    scheduled_for=scheduled_for,
                    now=execution_now,
                )
                if _elapsed_seconds(
                    execution_now,
                    job.created_at,
                ) >= settings.design_processing_readiness_alert_threshold_seconds:
                    log_design_processing_event(
                        logger,
                        "readiness_alert",
                        level=logging.WARNING,
                        board_id=item.board_id,
                        item_id=item.item_id,
                        job_id=str(job.id),
                        stage=job.stage,
                        readiness_check_count=job.readiness_check_count,
                        readiness_age_seconds=_elapsed_seconds(
                            execution_now,
                            job.created_at,
                        ),
                    )
                db.commit()
                return "readiness_wait"
            obligation = assign_next_execution(
                item,
                job,
                worker_id=worker_id,
                publication_allowed=publication_allowed,
                now=execution_now,
            )
            db.commit()
            if obligation is None:
                return "redundant"
        else:
            if job.execution_kind == "publication" and not publication_allowed:
                cancel_job(
                    item,
                    job,
                    reason="publication is not permitted by the current mode",
                    now=execution_now,
                    item_state="analyzed",
                )
                db.commit()
                return "cancelled"
            obligation = resume_execution(
                item,
                job,
                worker_id=worker_id,
                publication_allowed=publication_allowed,
                now=execution_now,
            )
            db.commit()

        stop_event, heartbeat_thread = _start_heartbeat(
            db,
            job_id,
            worker_id=worker_id,
            interval_seconds=heartbeat_interval_seconds,
        )
        try:
            if obligation == "publication":
                return run_publication_pipeline(
                    db,
                    job_id,
                    worker_id=worker_id,
                    gateway=gateway,
                    artifact_storage=artifact_storage,
                    pipeline_version=settings.design_processing_pipeline_version,
                    expected_board_id=str(settings.design_processing_board_id),
                    expected_group_id=str(settings.design_processing_landing_group_id),
                    mode=mode,
                    allowlist_item_ids=settings.design_processing_allowlist_item_ids,
                    execution_policy=execution_policy,
                )
            return run_analysis_pipeline(
                db,
                job_id,
                worker_id=worker_id,
                access_token=access_token,
                gateway=gateway,
                analysis_client=analysis_client,
                artifact_storage=artifact_storage,
                artifact_bucket=settings.design_processing_artifact_bucket,
                pipeline_version=settings.design_processing_pipeline_version,
                expected_board_id=str(settings.design_processing_board_id),
                expected_group_id=str(settings.design_processing_landing_group_id),
                mode=mode,
                allowlist_item_ids=settings.design_processing_allowlist_item_ids,
                execution_policy=execution_policy,
                downloader=downloader,
            )
        finally:
            _stop_heartbeat(stop_event, heartbeat_thread)
    except DesignProcessingTargetMismatch:
        db.rollback()
        return _cancel_or_replace_mismatched_job(
            db,
            job_id,
            worker_id=worker_id,
            gateway=gateway,
            mode=mode,
            execution_policy=execution_policy,
            now=utc_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Design-processing job %s failed", job_id)
        return _record_execution_failure(
            db,
            job_id,
            worker_id=worker_id,
            error=str(exc)[:2000],
            now=utc_now(),
        )


def execute_claimed_design_processing_job(
    db: Session,
    job_id: object,
    **kwargs,
) -> str:
    return execute_claimed_analysis_job(db, job_id, **kwargs)


def run_worker_once(
    db: Session,
    *,
    worker_id: Optional[str] = None,
    access_token: Optional[str] = None,
    gateway: Optional[DesignProcessingReadGateway] = None,
    analysis_client: Optional[LegacyGeminiClient] = None,
    artifact_storage: Optional[DesignArtifactStorage] = None,
    downloader: Optional[AssetDownloader] = None,
    mode: Optional[DesignProcessingMode] = None,
    execution_policy: Optional[ExecutionPolicy] = None,
    claim_limit: int = 1,
    recover_leases: bool = True,
    lease_timeout_seconds: int = 3600,
    heartbeat_interval_seconds: float = 60.0,
) -> DesignProcessingWorkerResult:
    configured_mode: DesignProcessingMode = mode or settings.design_processing_mode
    resolved_worker_id = worker_id or default_worker_id()
    runtime_policy = execution_policy
    if runtime_policy is None and mode is None:
        runtime_policy = lambda execution_kind, item_id: execution_allowed_for_item(
            mode=settings.design_processing_mode,
            execution_kind=execution_kind,
            item_id=item_id,
            allowlist_item_ids=settings.design_processing_allowlist_item_ids,
        )
    recovered = (
        recover_expired_design_processing_leases(
            db,
            lease_timeout_seconds=lease_timeout_seconds,
            mode=configured_mode,
            allowlist_item_ids=settings.design_processing_allowlist_item_ids,
        )
        if recover_leases
        else 0
    )
    if configured_mode == "off":
        result = DesignProcessingWorkerResult(
            recovered=recovered,
            cancelled=recovered,
        )
        _log_worker_batch(
            result,
            worker_id=resolved_worker_id,
            mode=configured_mode,
        )
        return result
    token = access_token or get_monday_ingestion_access_token()
    read_gateway = gateway or MondayDesignProcessingReadGateway(
        access_token=token,
        project_board_id=str(settings.design_processing_project_board_id),
    )
    client = analysis_client or LegacyGeminiClient(
        settings.design_processing_extraction_model
    )
    storage = artifact_storage or SupabaseDesignArtifactStorage()
    jobs = claim_due_design_processing_jobs(
        db,
        worker_id=resolved_worker_id,
        limit=claim_limit,
    )
    outcomes: dict[str, int] = {}
    for job in jobs:
        outcome = execute_claimed_design_processing_job(
            db,
            job.id,
            worker_id=resolved_worker_id,
            access_token=token,
            gateway=read_gateway,
            analysis_client=client,
            artifact_storage=storage,
            mode=configured_mode,
            execution_policy=runtime_policy,
            downloader=downloader,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        log_design_processing_event(
            logger,
            "worker_job_outcome",
            worker_id=resolved_worker_id,
            board_id=job.board_id,
            item_id=job.item_id,
            job_id=str(job.id),
            execution_kind=job.execution_kind,
            outcome=outcome,
        )
    result = DesignProcessingWorkerResult(
        recovered=recovered,
        claimed=len(jobs),
        analyzed=outcomes.get("analyzed", 0),
        published=outcomes.get("published", 0),
        readiness_wait=outcomes.get("readiness_wait", 0),
        redundant=outcomes.get("redundant", 0),
        retry_wait=outcomes.get("retry_wait", 0),
        failed=outcomes.get("failed", 0),
        cancelled=outcomes.get("cancelled", 0),
    )
    _log_worker_batch(
        result,
        worker_id=resolved_worker_id,
        mode=configured_mode,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the durable design-processing worker",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--worker-id")
    parser.add_argument("--claim-limit", type=int, default=1)
    parser.add_argument("--lease-timeout-seconds", type=int, default=3600)
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=60.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if not settings.design_processing_worker_enabled:
        logger.info("Design-processing worker is disabled")
        return 0
    worker_id = args.worker_id or default_worker_id()
    while True:
        db = SessionLocal()
        try:
            run_worker_once(
                db,
                worker_id=worker_id,
                claim_limit=args.claim_limit,
                lease_timeout_seconds=args.lease_timeout_seconds,
                heartbeat_interval_seconds=args.heartbeat_interval_seconds,
            )
        finally:
            db.close()
        if args.once:
            return 0
        time.sleep(max(args.poll_interval_seconds, 0.1))


if __name__ == "__main__":
    raise SystemExit(main())
