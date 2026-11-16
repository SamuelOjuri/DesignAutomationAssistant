"""Source email and retained archive failures, strictly read-only."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
from backend.app.config import settings
from backend.app.db import SessionLocal
from backend.app.services.legacy_enquiry.email_extraction import process_email_content

output = ROOT / "tmp" / "archive-3353889887"
output.mkdir(exist_ok=True)
emails = []
for label in ("a", "b"):
    path = Path(r"C:\Users\SamuelOjuri\Downloads") / f"3353889887 - {label}.eml"
    metadata = {}
    header, body, attachments, images = process_email_content(path.read_bytes(), path.name, source_metadata=metadata)
    email_text = header + "\n" + body
    (output / f"{label}-body.txt").write_text(email_text, encoding="utf-8")
    (output / f"{label}-metadata.json").write_text(json.dumps(metadata, ensure_ascii=True, indent=2), encoding="utf-8")
    emails.append({"label": label, "header": header, "body": body,
                   "body_sha256": hashlib.sha256(email_text.encode()).hexdigest(),
                   "attachments": [{"filename": a["filename"], "bytes": len(a["content"] or b"")}
                                   for a in attachments], "inline_images": len(images)})

with SessionLocal() as db:
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    db.execute(text("SET LOCAL statement_timeout = '15000ms'"))
    observed = db.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one()
    params = {"board": str(settings.design_processing_board_id), "item": "3353889887"}
    jobs = db.execute(text("""
        SELECT id, status, execution_kind, created_at, started_at, completed_at, attempt_count,
               execution_pipeline_version, left(last_error, 2000) AS last_error,
               result_json->'sourceExtractionFailure' AS source_failure,
               result_json->'specificationEvidenceFailure'->'fieldIssues' AS field_issues
        FROM design_processing_jobs WHERE board_id=:board AND item_id=:item
        ORDER BY created_at DESC LIMIT 12
    """), params).mappings().all()
    specifications = db.execute(text("""
        SELECT review_json, last_error, requested_generation, completed_generation,
               scheduled_for, current_revision, prepared_revision
        FROM performance_specifications WHERE board_id=:board AND item_id=:item
    """), params).mappings().all()
    item = db.execute(text("""
        SELECT state, latest_desired_input_revision, latest_analyzed_input_revision,
               latest_desired_pipeline_version, latest_analyzed_pipeline_version
        FROM design_processing_items WHERE board_id=:board AND item_id=:item
    """), params).mappings().all()
    db.rollback()

summaries = []
for raw in jobs:
    job = dict(raw)
    manifest = job.pop("source_failure") or []
    job["sources"] = [{key: source.get(key) for key in
                       ("sourceId", "filename", "status", "sizeBytes", "contentSha256", "parentSourceId", "error")}
                      for source in manifest if isinstance(source, dict)]
    summaries.append(job)
result = {"observed_at": observed, "emails": emails, "item": [dict(row) for row in item],
          "specification": [dict(row) for row in specifications], "jobs": summaries}
(output / "retained-summary.json").write_text(json.dumps(result, default=str, ensure_ascii=True, indent=2), encoding="utf-8")
print(json.dumps(result, default=str, ensure_ascii=True, indent=2))
