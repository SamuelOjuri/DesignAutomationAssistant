"""Offline real-package replay; image/model gateways are stubbed, no remote writes."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.performance_specification_contract import FIELD_NAMES, source_review_notes
from backend.app.services.specification_semantics import ARCHIVE_MEMBER_REVIEW_STATUS, MISSING_CLIENT_EMAIL_REVIEW_STATUS

path = Path(r"C:\Users\SamuelOjuri\Downloads\Westfield_Shepherd_s_Bush_2026-11-12_TaperedView_6001.zip")
digest = hashlib.sha256(path.read_bytes()).hexdigest()
assert digest == "64c3050e728281bc66a13ff64de8e381cd5bff2512f5c07df9bd7cf2d7d4c53c"
original_open = ZipFile.open
opened = []
def guarded_open(archive, entry, *args, **kwargs):
    name = entry.filename if hasattr(entry, "filename") else entry
    assert not name.lower().endswith((".obj", ".mtl"))
    opened.append(name)
    return original_open(archive, entry, *args, **kwargs)
ZipFile.open = guarded_open

calls = []
def extract(context, manifest):
    summary, = [record for record in manifest if record["filename"].endswith("project_summary.txt")]
    # This deterministic test gateway copies literal supplied metadata only. It
    # does not claim a live model run or infer any facts from the preview image.
    title = summary["text"].splitlines()[0]
    return DesignParameterExtraction(**{
        **{name: None for name in DesignParameterExtraction.model_fields if not name.startswith("specification_")},
        "drawing_title": title, "target_u_value": "0.15", "company": "Survey contact should not become requester",
        "date_received": "12 Nov 2026", "hour_received": "10:24", "specification_assessed_fields": list(FIELD_NAMES),
        "specification_evidence": [
            {"field": field, "value": value, "source_id": summary["sourceId"], "snippet": snippet,
             "location": "project_summary.txt", "scope": None, "issuer": None,
             "role": "requested", "request_role": "document",
             "rationale": "Literal survey metadata for offline validation"}
            for field, value, snippet in [("Drawing Title", title, title), ("Target U Value", "0.15", "U-Value: 0.15")]
        ],
    })

def image_stub(content, filename):
    calls.append(filename)
    return "VISUAL CONTEXT\nOffline image gateway placeholder; no image findings asserted."

download = SimpleNamespace(temp_path=str(path), content_sha256=digest, size_bytes=path.stat().st_size,
    source=SimpleNamespace(asset_id="1", filename=path.name, file_extension="zip"))
result = analyze_downloaded_email_assets([download], client=SimpleNamespace(
    process_image=image_stub, extract_design_parameters_with_manifest=extract,
    query_llm=lambda *args: (_ for _ in ()).throw(AssertionError("Verified title must be reused"))))
meshes = [record for record in result.source_manifest if record["status"] == ARCHIVE_MEMBER_REVIEW_STATUS]
missing = [record for record in result.source_manifest if record["status"] == MISSING_CLIENT_EMAIL_REVIEW_STATUS]
assert len(meshes) == 2 and len(missing) == 1
assert all(record["contentSha256"] is None for record in meshes)
assert result.performance_parameters["Company"] is result.performance_parameters["Date Received"] is None
assert result.specification_request_intent["recommended_type"] == "uncertain"
assert len(calls) == 1 and len(opened) == 4
print(json.dumps({"archive_hash_verified": True, "opened_supported_members": opened,
                  "unopened_mesh_members": [record["filename"] for record in meshes],
                  "project_title_verified": result.project_name,
                  "missing_email_issue": True, "client_request_date": result.performance_parameters["Date Received"],
                  "request_type": result.specification_request_intent["recommended_type"],
                  "source_reviews": source_review_notes(result.source_manifest),
                  "image_and_model_gateways_stubbed": True}, ensure_ascii=True, indent=2))
