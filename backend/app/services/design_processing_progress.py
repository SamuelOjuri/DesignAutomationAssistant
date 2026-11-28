"""Sanitized progress, fenced to the active analysis execution."""
from datetime import datetime, timezone
import json

from sqlalchemy import exists, text, update

from ..models import DesignProcessingItem, DesignProcessingJob


ANALYSIS_PROGRESS_PHASES = frozenset({"reading_sources", "extracting_fields", "matching", "rendering"})


def _result_with_progress(dialect_name, progress):
    # Patch only the progress key in the database. A retried job can retain a
    # ~0.6 MB failure payload, which must not be read and rewritten per update.
    patch = json.dumps(progress, separators=(",", ":"))
    if dialect_name == "postgresql":
        return text(
            "(COALESCE(NULLIF(result_json::jsonb, 'null'::jsonb), '{}'::jsonb)"
            " || jsonb_build_object('progress', CAST(:analysis_progress AS jsonb)))::json"
        ).bindparams(analysis_progress=patch)
    return text(
        "json_set(COALESCE(NULLIF(result_json, 'null'), '{}'), '$.progress', json(:analysis_progress))"
    ).bindparams(analysis_progress=patch)


def persist_analysis_progress(db, job_id, *, worker_id, progress):
    phase = progress.get("phase")
    if phase not in ANALYSIS_PROGRESS_PHASES:
        return
    counts = {key: value for key, value in progress.get("source_counts", {}).items()
              if key in {"total", "read", "skipped", "review", "failed"}
              and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10000}
    now = datetime.now(timezone.utc)
    item, job = DesignProcessingItem, DesignProcessingJob
    current_execution = exists().where(
        item.board_id == job.board_id,
        item.item_id == job.item_id,
        item.latest_desired_input_revision == job.execution_input_revision,
        item.latest_desired_pipeline_version == job.execution_pipeline_version,
    )
    statement = (
        update(job)
        .where(job.id == job_id, job.status == "running", job.locked_by == worker_id, current_execution)
        .values(
            result_json=_result_with_progress(db.get_bind().dialect.name, {
                "phase": phase, "source_counts": counts, "updated_at": now.isoformat(),
            }),
            heartbeat_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if db.execute(statement).rowcount != 1:
        db.rollback()
        raise RuntimeError("Analysis progress belongs to an obsolete execution")
    db.commit()
    loaded = db.identity_map.get(db.identity_key(job, job_id))
    if loaded is not None:
        db.expire(loaded, ["result_json", "heartbeat_at", "updated_at"])
