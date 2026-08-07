from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import logging
import math
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from ..models import (
    DESIGN_PROCESSING_ACTIVE_JOB_STATUSES,
    DesignProcessingArtifact,
    DesignProcessingItem,
    DesignProcessingJob,
    MondayWebhookDispatch,
)
from .auto_sync import utc_now


def log_design_processing_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    payload = {
        "event": event,
        "timestamp": utc_now().isoformat(),
        **fields,
    }
    logger.log(
        level,
        "design_processing_event=%s",
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
    )


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age_seconds(now: datetime, value: Optional[datetime]) -> Optional[float]:
    if value is None:
        return None
    return round(max(0.0, (_as_aware_utc(now) - _as_aware_utc(value)).total_seconds()), 3)


def _duration_seconds(
    start: Optional[datetime],
    end: Optional[datetime],
) -> Optional[float]:
    if start is None or end is None:
        return None
    return round(max(0.0, (_as_aware_utc(end) - _as_aware_utc(start)).total_seconds()), 3)


def _percentile(values: Iterable[float], percentile: float) -> Optional[float]:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(float(ordered[index]), 3)


def _counter(values: Iterable[Optional[str]]) -> dict[str, int]:
    return dict(sorted(Counter(value for value in values if value is not None).items()))


def collect_design_processing_metrics(
    db: Session,
    *,
    now: Optional[datetime] = None,
    lease_timeout_seconds: int = 3600,
) -> dict[str, Any]:
    if lease_timeout_seconds < 1:
        raise ValueError("lease_timeout_seconds must be positive")
    collected_at = now or utc_now()

    items = db.query(
        DesignProcessingItem.state,
        DesignProcessingItem.latest_desired_input_revision,
        DesignProcessingItem.latest_desired_pipeline_version,
        DesignProcessingItem.latest_analyzed_input_revision,
        DesignProcessingItem.latest_analyzed_pipeline_version,
        DesignProcessingItem.latest_published_input_revision,
        DesignProcessingItem.latest_published_pipeline_version,
        DesignProcessingItem.supersession_requested_at,
    ).all()
    jobs = db.query(
        DesignProcessingJob.status,
        DesignProcessingJob.stage,
        DesignProcessingJob.execution_kind,
        DesignProcessingJob.attempt_count,
        DesignProcessingJob.readiness_check_count,
        DesignProcessingJob.locked_at,
        DesignProcessingJob.heartbeat_at,
        DesignProcessingJob.started_at,
        DesignProcessingJob.completed_at,
        DesignProcessingJob.created_at,
        DesignProcessingJob.superseded_by_revision,
    ).all()
    artifacts = db.query(
        DesignProcessingArtifact.status,
        DesignProcessingArtifact.last_error,
    ).all()
    dispatches = db.query(
        MondayWebhookDispatch.status,
        MondayWebhookDispatch.outcome,
    ).filter(MondayWebhookDispatch.consumer == "design_processing").all()

    active_jobs = [job for job in jobs if job.status in DESIGN_PROCESSING_ACTIVE_JOB_STATUSES]
    readiness_jobs = [
        job
        for job in active_jobs
        if job.execution_kind is None
        and job.stage in {"waiting_for_name", "waiting_for_email"}
    ]
    running_jobs = [job for job in jobs if job.status == "running"]
    heartbeat_ages = [
        age
        for job in running_jobs
        if (age := _age_seconds(collected_at, job.heartbeat_at or job.locked_at))
        is not None
    ]
    publication_latencies = [
        latency
        for job in jobs
        if job.execution_kind == "publication" and job.status == "completed"
        if (latency := _duration_seconds(job.started_at, job.completed_at)) is not None
    ]
    attempt_counts = [float(job.attempt_count or 0) for job in jobs]
    readiness_counts = [float(job.readiness_check_count or 0) for job in readiness_jobs]
    analyzed_not_published = sum(
        1
        for item in items
        if item.latest_desired_input_revision is not None
        and (
            item.latest_desired_input_revision,
            item.latest_desired_pipeline_version,
        )
        == (
            item.latest_analyzed_input_revision,
            item.latest_analyzed_pipeline_version,
        )
        and (
            item.latest_desired_input_revision,
            item.latest_desired_pipeline_version,
        )
        != (
            item.latest_published_input_revision,
            item.latest_published_pipeline_version,
        )
    )

    return {
        "schemaVersion": 1,
        "collectedAt": _as_aware_utc(collected_at).isoformat(),
        "queueDepth": _counter(job.status for job in active_jobs),
        "itemsByState": _counter(item.state for item in items),
        "readiness": {
            "waitingJobs": len(readiness_jobs),
            "oldestAgeSeconds": max(
                (_age_seconds(collected_at, job.created_at) or 0.0 for job in readiness_jobs),
                default=None,
            ),
            "checksTotal": sum(int(value) for value in readiness_counts),
            "checksP95": _percentile(readiness_counts, 0.95),
        },
        "attempts": {
            "jobs": len(jobs),
            "total": sum(int(value) for value in attempt_counts),
            "p50": _percentile(attempt_counts, 0.50),
            "p95": _percentile(attempt_counts, 0.95),
        },
        "leases": {
            "running": len(running_jobs),
            "expired": sum(age >= lease_timeout_seconds for age in heartbeat_ages),
            "oldestHeartbeatAgeSeconds": max(heartbeat_ages, default=None),
        },
        "supersessions": {
            "pendingItems": sum(item.supersession_requested_at is not None for item in items),
            "cancelledJobs": sum(
                job.status == "cancelled" and job.superseded_by_revision is not None
                for job in jobs
            ),
        },
        "analyzedNotPublished": analyzed_not_published,
        "publicationLatencySeconds": {
            "count": len(publication_latencies),
            "p50": _percentile(publication_latencies, 0.50),
            "p95": _percentile(publication_latencies, 0.95),
        },
        "artifactCleanup": {
            "deletePending": sum(artifact.status == "delete_pending" for artifact in artifacts),
            "deletePendingWithErrors": sum(
                artifact.status == "delete_pending" and bool(artifact.last_error)
                for artifact in artifacts
            ),
            "deleted": sum(artifact.status == "deleted" for artifact in artifacts),
        },
        "webhookChildren": {
            "status": _counter(dispatch.status for dispatch in dispatches),
            "outcome": _counter(dispatch.outcome for dispatch in dispatches),
        },
    }