from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from ..models import (
    DESIGN_PROCESSING_ACTIVE_JOB_STATUSES,
    DESIGN_PROCESSING_JOB_STAGES,
    DesignProcessingArtifact,
    DesignProcessingItem,
    DesignProcessingJob,
)


AI_DATA_COLUMN_ID = "file_mkza7y37"
MATCH_REPORT_COLUMN_ID = "file_mm59rntf"

ANALYSIS_STAGES = frozenset({"extracting", "matching", "rendering"})
PUBLICATION_STAGES = frozenset(
    {
        "writing_columns",
        "uploading_ai_data",
        "uploading_ai_data_pdf",
        "uploading_match_report",
    }
)


class InvalidDesignProcessingTransition(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessingIdentity:
    input_revision: str
    pipeline_version: str

    def __post_init__(self) -> None:
        if not self.input_revision:
            raise ValueError("input_revision must not be empty")
        if not self.pipeline_version:
            raise ValueError("pipeline_version must not be empty")


def _identity_from_values(
    input_revision: Optional[str],
    pipeline_version: Optional[str],
    *,
    field_name: str,
) -> Optional[ProcessingIdentity]:
    if input_revision is None and pipeline_version is None:
        return None
    if input_revision is None or pipeline_version is None:
        raise InvalidDesignProcessingTransition(
            f"{field_name} identity must be fully null or fully populated"
        )
    return ProcessingIdentity(input_revision, pipeline_version)


def desired_identity(item: DesignProcessingItem) -> Optional[ProcessingIdentity]:
    return _identity_from_values(
        item.latest_desired_input_revision,
        item.latest_desired_pipeline_version,
        field_name="desired",
    )


def analyzed_identity(item: DesignProcessingItem) -> Optional[ProcessingIdentity]:
    return _identity_from_values(
        item.latest_analyzed_input_revision,
        item.latest_analyzed_pipeline_version,
        field_name="analyzed",
    )


def published_identity(item: DesignProcessingItem) -> Optional[ProcessingIdentity]:
    return _identity_from_values(
        item.latest_published_input_revision,
        item.latest_published_pipeline_version,
        field_name="published",
    )


def execution_identity(job: DesignProcessingJob) -> Optional[ProcessingIdentity]:
    identity = _identity_from_values(
        job.execution_input_revision,
        job.execution_pipeline_version,
        field_name="execution",
    )
    if (job.execution_kind is None) != (identity is None):
        raise InvalidDesignProcessingTransition(
            "execution kind and identity must be assigned together"
        )
    return identity


def needs_analysis(item: DesignProcessingItem) -> bool:
    desired = desired_identity(item)
    return desired is not None and desired != analyzed_identity(item)


def needs_publication(
    item: DesignProcessingItem,
    *,
    publication_allowed: bool,
) -> bool:
    desired = desired_identity(item)
    return (
        publication_allowed
        and desired is not None
        and desired == analyzed_identity(item)
        and desired != published_identity(item)
    )


def next_obligation(
    item: DesignProcessingItem,
    *,
    publication_allowed: bool,
) -> Optional[str]:
    if needs_analysis(item):
        return "analysis"
    if needs_publication(item, publication_allowed=publication_allowed):
        return "publication"
    return None


def update_desired_identity(
    item: DesignProcessingItem,
    identity: Optional[ProcessingIdentity],
    *,
    now: datetime,
    active_job: Optional[DesignProcessingJob] = None,
) -> bool:
    previous = desired_identity(item)
    if previous == identity:
        return False

    item.latest_desired_input_revision = (
        identity.input_revision if identity is not None else None
    )
    item.latest_desired_pipeline_version = (
        identity.pipeline_version if identity is not None else None
    )
    item.updated_at = now

    running_execution = None
    if active_job is not None and active_job.status == "running":
        running_execution = execution_identity(active_job)
        if running_execution is not None and running_execution != identity:
            item.supersession_requested_at = now

    if identity is not None and identity != published_identity(item):
        if active_job is not None and active_job.status == "running":
            item.state = (
                "publishing"
                if active_job.execution_kind == "publication"
                else "processing"
            )
        else:
            item.state = "scheduled"
    return True


def claim_job(
    job: DesignProcessingJob,
    *,
    worker_id: str,
    now: datetime,
) -> None:
    if job.status not in {"scheduled", "retry_wait"}:
        raise InvalidDesignProcessingTransition(
            f"cannot claim job in {job.status!r} status"
        )
    if not worker_id:
        raise ValueError("worker_id must not be empty")

    job.status = "running"
    job.locked_by = worker_id
    job.locked_at = now
    job.heartbeat_at = now
    job.started_at = job.started_at or now
    job.next_retry_at = None
    job.updated_at = now


def transition_to_readiness_wait(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    missing_name: bool,
    missing_email: bool,
    scheduled_for: datetime,
    now: datetime,
) -> None:
    if not missing_name and not missing_email:
        raise ValueError("at least one readiness input must be missing")
    if execution_identity(job) is not None:
        raise InvalidDesignProcessingTransition(
            "an assigned execution must be cancelled before readiness waiting"
        )
    if job.status not in DESIGN_PROCESSING_ACTIVE_JOB_STATUSES:
        raise InvalidDesignProcessingTransition(
            f"cannot reschedule terminal job in {job.status!r} status"
        )

    update_desired_identity(item, None, now=now, active_job=job)
    item.state = "waiting_for_name" if missing_name else "waiting_for_email"
    item.updated_at = now

    job.status = "retry_wait"
    job.stage = "waiting_for_name" if missing_name else "waiting_for_email"
    job.scheduled_for = scheduled_for
    job.next_retry_at = scheduled_for
    job.readiness_check_count += 1
    job.last_error = None
    _clear_lease(job)
    job.updated_at = now


def assign_next_execution(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    worker_id: str,
    publication_allowed: bool,
    now: datetime,
) -> Optional[str]:
    _require_lease(job, worker_id)
    if execution_identity(job) is not None:
        raise InvalidDesignProcessingTransition("execution identity is already assigned")

    identity = desired_identity(item)
    if identity is None:
        raise InvalidDesignProcessingTransition(
            "cannot assign execution without a desired identity"
        )
    obligation = next_obligation(item, publication_allowed=publication_allowed)
    if obligation is None:
        _complete_job(job, now=now)
        if (
            identity == analyzed_identity(item)
            and identity != published_identity(item)
        ):
            item.state = "analyzed"
            item.updated_at = now
        return None

    _increment_attempt(job)
    job.execution_kind = obligation
    job.execution_input_revision = identity.input_revision
    job.execution_pipeline_version = identity.pipeline_version
    job.stage = "extracting" if obligation == "analysis" else "writing_columns"
    job.updated_at = now
    item.state = "processing" if obligation == "analysis" else "publishing"
    item.updated_at = now
    return obligation


def resume_execution(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    worker_id: str,
    publication_allowed: bool,
    now: datetime,
) -> str:
    _require_lease(job, worker_id)
    identity = execution_identity(job)
    if identity is None or job.execution_kind is None:
        raise InvalidDesignProcessingTransition("execution has not been assigned")
    if identity != desired_identity(item):
        raise InvalidDesignProcessingTransition("execution identity has been superseded")
    if job.execution_kind == "publication" and not publication_allowed:
        raise InvalidDesignProcessingTransition("publication is no longer allowed")

    _increment_attempt(job)
    item.state = "processing" if job.execution_kind == "analysis" else "publishing"
    item.updated_at = now
    job.updated_at = now
    return job.execution_kind


def advance_stage(
    job: DesignProcessingJob,
    stage: str,
    *,
    worker_id: str,
    now: datetime,
) -> None:
    _require_lease(job, worker_id)
    if stage not in DESIGN_PROCESSING_JOB_STAGES:
        raise ValueError(f"unknown design-processing stage {stage!r}")
    allowed_stages = (
        ANALYSIS_STAGES if job.execution_kind == "analysis" else PUBLICATION_STAGES
    )
    if stage not in allowed_stages:
        raise InvalidDesignProcessingTransition(
            f"stage {stage!r} is invalid for {job.execution_kind!r} execution"
        )
    job.stage = stage
    job.updated_at = now


def complete_analysis(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    worker_id: str,
    now: datetime,
) -> None:
    identity = _require_current_execution(
        item,
        job,
        worker_id=worker_id,
        execution_kind="analysis",
    )
    item.latest_analyzed_input_revision = identity.input_revision
    item.latest_analyzed_pipeline_version = identity.pipeline_version
    item.state = "analyzed"
    item.supersession_requested_at = None
    item.updated_at = now
    _complete_job(job, now=now)


def complete_publication(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    artifacts: Iterable[DesignProcessingArtifact],
    *,
    worker_id: str,
    now: datetime,
) -> None:
    identity = _require_current_execution(
        item,
        job,
        worker_id=worker_id,
        execution_kind="publication",
    )
    if analyzed_identity(item) != identity:
        raise InvalidDesignProcessingTransition(
            "publication identity has not completed analysis"
        )
    artifact_list = list(artifacts)
    if not _has_published_artifacts(item, identity, artifact_list):
        raise InvalidDesignProcessingTransition(
            "all current artifacts must be published with Monday asset IDs"
        )

    item.latest_published_input_revision = identity.input_revision
    item.latest_published_pipeline_version = identity.pipeline_version
    if not is_ready_for_review(item, artifact_list):
        raise InvalidDesignProcessingTransition("readiness predicate is not satisfied")
    item.state = "ready_for_review"
    item.supersession_requested_at = None
    item.updated_at = now
    _complete_job(job, now=now)


def schedule_execution_retry(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    scheduled_for: datetime,
    error: str,
    now: datetime,
) -> None:
    identity = execution_identity(job)
    if identity is None or job.execution_kind is None:
        raise InvalidDesignProcessingTransition(
            "normal processing retries require an assigned execution"
        )
    if job.status != "running":
        raise InvalidDesignProcessingTransition(
            f"cannot retry job in {job.status!r} status"
        )

    job.status = "retry_wait"
    job.scheduled_for = scheduled_for
    job.next_retry_at = scheduled_for
    job.last_error = error
    _clear_lease(job)
    job.updated_at = now
    item.state = "processing" if job.execution_kind == "analysis" else "publishing"
    item.updated_at = now


def fail_job(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    error: str,
    now: datetime,
) -> None:
    if job.status not in DESIGN_PROCESSING_ACTIVE_JOB_STATUSES:
        raise InvalidDesignProcessingTransition(
            f"cannot fail terminal job in {job.status!r} status"
        )
    job.status = "failed"
    job.last_error = error
    job.completed_at = now
    _clear_lease(job)
    job.updated_at = now
    item.state = "failed"
    item.updated_at = now


def cancel_job(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    reason: str,
    now: datetime,
    item_state: str,
    superseded_by_revision: Optional[str] = None,
) -> None:
    if job.status not in DESIGN_PROCESSING_ACTIVE_JOB_STATUSES:
        raise InvalidDesignProcessingTransition(
            f"cannot cancel terminal job in {job.status!r} status"
        )
    if item_state not in {
        "waiting_for_name",
        "waiting_for_email",
        "scheduled",
        "analyzed",
        "ineligible",
    }:
        raise ValueError(f"invalid cancellation item state {item_state!r}")

    job.status = "cancelled"
    job.last_error = reason
    job.superseded_by_revision = superseded_by_revision
    job.completed_at = now
    _clear_lease(job)
    job.updated_at = now
    item.state = item_state
    item.updated_at = now


def cancel_superseded_execution(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    now: datetime,
) -> None:
    identity = execution_identity(job)
    desired = desired_identity(item)
    if identity is None:
        raise InvalidDesignProcessingTransition("job has no execution to supersede")
    if desired == identity:
        raise InvalidDesignProcessingTransition("execution identity is still current")

    cancel_job(
        item,
        job,
        reason="execution superseded by latest desired identity",
        now=now,
        item_state="scheduled" if desired is not None else "waiting_for_email",
        superseded_by_revision=(desired.input_revision if desired is not None else None),
    )


def is_ready_for_review(
    item: DesignProcessingItem,
    artifacts: Iterable[DesignProcessingArtifact],
) -> bool:
    desired = desired_identity(item)
    published = published_identity(item)
    return (
        desired is not None
        and desired == published
        and _has_published_artifacts(item, published, artifacts)
    )


def _has_published_artifacts(
    item: DesignProcessingItem,
    identity: ProcessingIdentity,
    artifacts: Iterable[DesignProcessingArtifact],
) -> bool:
    required = {
        ("ai_data", AI_DATA_COLUMN_ID),
        ("ai_data_pdf", AI_DATA_COLUMN_ID),
        ("match_report", MATCH_REPORT_COLUMN_ID),
    }
    present = {
        (artifact.artifact_kind, artifact.column_id)
        for artifact in artifacts
        if artifact.board_id == item.board_id
        and artifact.item_id == item.item_id
        and artifact.input_revision == identity.input_revision
        and artifact.pipeline_version == identity.pipeline_version
        and artifact.status == "published"
        and artifact.monday_asset_id is not None
    }
    return required <= present


def _require_lease(job: DesignProcessingJob, worker_id: str) -> None:
    if job.status != "running" or job.locked_by != worker_id:
        raise InvalidDesignProcessingTransition("worker does not own the running job lease")


def _require_current_execution(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    worker_id: str,
    execution_kind: str,
) -> ProcessingIdentity:
    _require_lease(job, worker_id)
    identity = execution_identity(job)
    if identity is None or job.execution_kind != execution_kind:
        raise InvalidDesignProcessingTransition(
            f"job is not an assigned {execution_kind} execution"
        )
    if identity != desired_identity(item):
        raise InvalidDesignProcessingTransition("execution identity has been superseded")
    return identity


def _increment_attempt(job: DesignProcessingJob) -> None:
    if job.attempt_count >= job.max_attempts:
        raise InvalidDesignProcessingTransition("job has exhausted its normal attempts")
    job.attempt_count += 1


def _clear_lease(job: DesignProcessingJob) -> None:
    job.locked_at = None
    job.locked_by = None
    job.heartbeat_at = None


def _complete_job(job: DesignProcessingJob, *, now: datetime) -> None:
    job.status = "completed"
    job.completed_at = now
    job.next_retry_at = None
    job.last_error = None
    _clear_lease(job)
    job.updated_at = now