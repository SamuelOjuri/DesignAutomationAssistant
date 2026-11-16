"""Offline replay of the retained rejected note against its supplied source email."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.services.legacy_enquiry.email_extraction import process_email_content
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.legacy_enquiry.source_documents import source_record
from backend.app.services.performance_specification_evidence import request_note_diagnostics
from backend.app.services.specification_recovery import extraction_payload, repair_note_prose_dashes

retained = json.loads((ROOT / "tmp/3345815639_retained_note_readonly.json").read_text(encoding="utf-8"))
job = next(job for job in retained["jobs"] if job.get("noteIssues"))
bad, good = job["notes"][2], job["notes"][4]
path = Path(r"C:\Users\SamuelOjuri\Downloads\3345815639 - a.eml")
metadata = {}
header, body, _, _ = process_email_content(path.read_bytes(), path.name, source_metadata=metadata)
text = header + "\n" + body
source = source_record(bad["source_id"], "Email body", text.encode(), source_type="email_body", text=text)
source.update(metadata)
expected_hash = next(entry["contentSha256"] for entry in job["noteSources"] if entry["sourceId"] == source["sourceId"])
assert hashlib.sha256(text.encode()).hexdigest() == expected_hash
result = DesignParameterExtraction(**{
    **{name: None for name in DesignParameterExtraction.model_fields if not name.startswith("specification_")},
    "specification_request_notes": [bad, good],
})
before = request_note_diagnostics(extraction_payload(result, [source]))
repair_note_prose_dashes(result, [source])
after = request_note_diagnostics(extraction_payload(result, [source]))
assert before == {0: "Request note has an unverified source/excerpt"}
assert after == {} and len(result.specification_request_notes) == 1
assert result.specification_request_notes[0].model_dump() == good
assert len(result._evidence_repairs) == 1
print(json.dumps({"source_body_hash_matches": True, "before_note_issues": before,
                  "after_note_issues": after, "retained_notes": len(result.specification_request_notes),
                  "audited_repairs": len(result._evidence_repairs),
                  "exact_corrected_quote_restored": True}, ensure_ascii=True))
