"""Correlate the supplied Render failures with current records, read-only."""
from pathlib import Path
import json
import re
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import bindparam, text
from backend.app.db import SessionLocal

LOG = Path(r"C:\Users\SamuelOjuri\.codex\attachments\02557e5d-804d-4838-b954-f4417baf4492\Pasted text.txt")
job_ids = sorted({uuid.UUID(value) for value in re.findall(
    r"Design-processing job ([a-f0-9-]+) failed", LOG.read_text(encoding="utf-8")
)})
item_ids = ["3296781921", "3312632285", "3312713363", "3312801232", "3312970498",
            "3315502554", "3316985606", "3319900903", "3319921927", "3320649967"]

try:
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '15000ms'"))
        observed_at = db.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one()
        jobs = db.execute(text("""
            SELECT id, item_id, status, execution_kind, execution_input_revision,
                   execution_pipeline_version, attempt_count, max_attempts,
                   started_at, completed_at, next_retry_at, left(last_error, 900) AS last_error
            FROM design_processing_jobs WHERE id IN :job_ids ORDER BY created_at, id
        """).bindparams(bindparam("job_ids", expanding=True)), {"job_ids": job_ids}).mappings().all()
        specifications = db.execute(text("""
            SELECT i.item_id, i.state,
                   (i.latest_desired_input_revision = i.latest_analyzed_input_revision
                    AND i.latest_analyzed_input_revision = i.extracted_parameters_json->>'inputRevision'
                    AND i.latest_desired_pipeline_version = i.latest_analyzed_pipeline_version
                    AND i.latest_analyzed_pipeline_version = i.extracted_parameters_json->>'pipelineVersion')
                        AS extraction_identities_match,
                   s.requested_generation, s.completed_generation, s.checked_at,
                   s.scheduled_for, s.lease_until, s.attempt_count,
                   s.review_json->>'status' AS review_status, left(s.last_error, 500) AS last_error,
                   a.status AS artifact_status, a.monday_asset_id,
                   (a.source_json->>'emailRevision' = i.latest_analyzed_input_revision
                    AND a.source_json->>'extractionPipeline' = i.latest_analyzed_pipeline_version)
                        AS artifact_matches_extraction
            FROM design_processing_items i
            LEFT JOIN performance_specifications s ON s.board_id=i.board_id AND s.item_id=i.item_id
            LEFT JOIN performance_specification_artifacts a
              ON a.board_id=s.board_id AND a.item_id=s.item_id AND a.revision=s.current_revision
            WHERE i.board_id='1882196103' AND i.item_id IN :item_ids ORDER BY i.item_id
        """).bindparams(bindparam("item_ids", expanding=True)), {"item_ids": item_ids}).mappings().all()
        db.rollback()
    print(json.dumps({"observed_at": observed_at, "jobs": [dict(row) for row in jobs],
                      "specifications": [dict(row) for row in specifications]}, default=str, indent=2))
except Exception as exc:
    print(json.dumps({"read_only_check_failed": type(exc).__name__,
                      "cause_type": type(getattr(exc, "orig", exc)).__name__,
                      "sqlstate": getattr(getattr(exc, "orig", None), "pgcode", None)}))
    sys.exit(1)
