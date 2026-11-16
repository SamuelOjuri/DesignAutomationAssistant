"""Read-only, item-scoped retained request-note diagnostic; no provider calls."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
from backend.app.config import settings
from backend.app.db import SessionLocal

try:
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '15000ms'"))
        observed = db.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one()
        parameters = {"board": str(settings.design_processing_board_id), "item": "3345815639"}
        jobs = db.execute(text("""
            SELECT id, status, execution_kind, execution_pipeline_version, started_at,
                   completed_at, created_at, attempt_count, left(last_error, 1000) AS last_error,
                   result_json->'specificationEvidenceFailure' AS evidence_failure,
                   result_json->'sourceExtractionFailure' AS source_failure
            FROM design_processing_jobs
            WHERE board_id=:board AND item_id=:item
            ORDER BY created_at DESC LIMIT 20
        """), parameters).mappings().all()
        specification = db.execute(text("""
            SELECT requested_generation, completed_generation, scheduled_for, lease_until,
                   checked_at, attempt_count, last_error, review_json, current_revision, prepared_revision
            FROM performance_specifications WHERE board_id=:board AND item_id=:item
        """), parameters).mappings().all()
        item = db.execute(text("""
            SELECT state, latest_desired_input_revision, latest_analyzed_input_revision,
                   latest_desired_pipeline_version, latest_analyzed_pipeline_version
            FROM design_processing_items WHERE board_id=:board AND item_id=:item
        """), parameters).mappings().all()
        db.rollback()
    summarized = []
    for raw in jobs:
        job = dict(raw)
        failure = job.pop("evidence_failure") or {}
        source_failure = job.pop("source_failure") or []
        extraction = failure.get("extraction") or {}
        notes = extraction.get("specificationRequestNotes") or []
        issues = failure.get("noteIssues") or {}
        job["fieldIssues"] = failure.get("fieldIssues")
        job["noteIssues"] = issues
        job["notes"] = notes
        manifest = extraction.get("sourceManifest") or failure.get("sourceManifest") or []
        note_sources = {note.get("source_id") for note in notes}
        job["noteSources"] = [{key: source.get(key) for key in
            ("sourceId", "filename", "sourceType", "status", "observedAt", "contentSha256", "parentSourceId")}
            for source in manifest if source.get("sourceId") in note_sources]
        job["sourceFailures"] = [{key: source.get(key) for key in ("sourceId", "filename", "status", "error")}
                                 for source in source_failure if isinstance(source, dict) and source.get("error")]
        summarized.append(job)
    result = {"observed_at": observed, "item": [dict(row) for row in item],
              "specification": [dict(row) for row in specification], "jobs": summarized}
    serialized = json.dumps(result, default=str, indent=2, ensure_ascii=False)
    (ROOT / "tmp" / "3345815639_retained_note_readonly.json").write_text(serialized, encoding="utf-8")
    print(serialized)
except Exception as exc:
    print(json.dumps({"read_only_check_failed": type(exc).__name__,
                      "cause_type": type(getattr(exc, "orig", exc)).__name__,
                      "sqlstate": getattr(getattr(exc, "orig", None), "pgcode", None)}))
    raise SystemExit(1)
