"""Read-only retained timestamp diagnostics and local source-email comparison."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
from backend.app.config import settings
from backend.app.db import SessionLocal
from backend.app.services.legacy_enquiry.email_extraction import process_email_content
from backend.app.services.performance_specification_evidence import resolve_email_evidence
from backend.app.services.specification_recovery import repair_message_dates
from backend.app.services.specification_request_context import source_email_messages
from backend.app.services.specification_semantics import SpecificationEvidence

out = ROOT / "tmp" / "timestamp-3352626143"
out.mkdir(exist_ok=True)
emails = []
for label in ("a", "b"):
    path = Path(r"C:\Users\SamuelOjuri\Downloads") / f"3352626143 - {label}.eml"
    metadata = {}
    header, body, attachments, images = process_email_content(path.read_bytes(), path.name, source_metadata=metadata)
    body_text = header + "\n" + body
    source = {"sourceId": label, "sourceType": "email_body", "status": "read", "text": body_text, **metadata}
    (out / f"{label}-body.txt").write_text(body_text, encoding="utf-8")
    (out / f"{label}-metadata.json").write_text(json.dumps(metadata, ensure_ascii=True, indent=2), encoding="utf-8")
    emails.append({"label": label, "header": header, "body_sha256": hashlib.sha256(body_text.encode()).hexdigest(),
                   "body": body, "messages": [{k: v for k, v in message.items() if k != "text"}
                                               for message in source_email_messages(source, include_plain=True)],
                   "attachments": [{"filename": a["filename"], "bytes": len(a["content"] or b"")}
                                   for a in attachments], "inline_images": len(images)})

with SessionLocal() as db:
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    db.execute(text("SET LOCAL statement_timeout = '15000ms'"))
    observed = db.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one()
    params = {"board": str(settings.design_processing_board_id), "item": "3352626143"}
    jobs = db.execute(text("""
        SELECT id, status, execution_kind, created_at, started_at, completed_at, attempt_count,
               execution_pipeline_version, left(last_error, 1600) AS last_error,
               result_json->'specificationEvidenceFailure' AS evidence_failure
        FROM design_processing_jobs WHERE board_id=:board AND item_id=:item
        ORDER BY created_at DESC LIMIT 12
    """), params).mappings().all()
    spec = db.execute(text("""
        SELECT review_json, last_error, requested_generation, completed_generation, scheduled_for,
               current_revision, prepared_revision
        FROM performance_specifications WHERE board_id=:board AND item_id=:item
    """), params).mappings().all()
    state = db.execute(text("""
        SELECT state, latest_desired_input_revision, latest_analyzed_input_revision,
               latest_desired_pipeline_version, latest_analyzed_pipeline_version
        FROM design_processing_items WHERE board_id=:board AND item_id=:item
    """), params).mappings().all()
    db.rollback()

summaries = []
for row in jobs:
    job = dict(row)
    failure = job.pop("evidence_failure") or {}
    if failure:
        extraction = failure.get("extraction") or {}
        (out / f"failure-{job['id']}.json").write_text(json.dumps(failure, ensure_ascii=True, indent=2), encoding="utf-8")
        job.update(fieldIssues=failure.get("fieldIssues"), noteIssues=failure.get("noteIssues"))
        if extraction:
            manifest = extraction.get("sourceManifest") or []
            fields = set(failure.get("fieldIssues") or {})
            fields.update(("Membrane", "VCL", "Decking"))
            job["evidence"] = [e for e in extraction.get("specificationEvidence", []) if e["field"] in fields]
            source_ids = {e["source_id"] for e in job["evidence"]}
            job["sources"] = [{key: s.get(key) for key in
                              ("sourceId", "filename", "sourceType", "status", "observedAt", "contentSha256")}
                             for s in manifest if s["sourceId"] in source_ids]
            for source in job["sources"]:
                source["supplied_email_match"] = next((e["label"] for e in emails if e["body_sha256"] == source["contentSha256"]), None)
            _, provenance, issues = resolve_email_evidence(extraction)
            job["current_resolver_issues"] = issues
            job["current_resolver_provenance"] = {field: provenance.get(field) for field in fields if field in provenance}
            result = SimpleNamespace(specification_evidence=[SpecificationEvidence.model_validate(e)
                                     for e in extraction.get("specificationEvidence", [])], _evidence_repairs=[])
            repair_message_dates(result, manifest)
            replay = {**extraction, "specificationEvidence": [e.model_dump() for e in result.specification_evidence]}
            raw, provenance, issues = resolve_email_evidence(replay)
            job["after_date_repair_issues"] = issues
            job["date_repairs"] = result._evidence_repairs
            job["replayed_values"] = {field: raw.get(field) for field in fields}
    summaries.append(job)

output = {"observed_at": str(observed), "emails": emails, "item": [dict(r) for r in state],
          "specification": [dict(r) for r in spec], "jobs": summaries}
(out / "summary.json").write_text(json.dumps(output, default=str, ensure_ascii=True, indent=2), encoding="utf-8")
print(json.dumps(output, default=str, ensure_ascii=True, indent=2))
