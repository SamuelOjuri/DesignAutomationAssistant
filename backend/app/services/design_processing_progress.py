"""Sanitized progress, fenced to the active analysis execution."""
from datetime import datetime, timezone

from .design_processing_locks import lock_design_processing_item_and_job
from .design_processing_state import desired_identity, execution_identity


def persist_analysis_progress(db, job_id, *, worker_id, progress):
    item, job = lock_design_processing_item_and_job(db, job_id)
    if (item is None or job is None or job.status != "running" or job.locked_by != worker_id
            or execution_identity(job) != desired_identity(item)):
        db.rollback()
        raise RuntimeError("Analysis progress belongs to an obsolete execution")
    phase = progress.get("phase")
    if phase not in {"reading_sources", "extracting_fields", "matching", "rendering"}:
        db.rollback()
        return
    counts = {key: value for key, value in progress.get("source_counts", {}).items()
              if key in {"total", "read", "skipped", "review", "failed"}
              and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10000}
    now = datetime.now(timezone.utc)
    job.result_json = {**(job.result_json or {}), "progress": {
        "phase": phase, "source_counts": counts, "updated_at": now.isoformat(),
    }}
    job.heartbeat_at = job.updated_at = now
    db.commit()
