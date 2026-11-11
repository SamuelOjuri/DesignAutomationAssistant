"""Summarize a failed diagnostic extraction; never certify it as valid output."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.services.performance_specification_evidence import (
    resolve_email_evidence, resolve_request_notes,
)
from backend.app.services.specification_semantics import PDF_FIELD_NAMES

folder = Path(__file__).resolve().parent
failure = json.loads((folder / "diagnostic_error.json").read_text(encoding="utf-8"))
details = failure["evidence_failure"]
extraction = details["extraction"]
resolved, provenance, issues = resolve_email_evidence(extraction)
summary = {
    "status": "REJECTED_DIAGNOSTIC_EXTRACTION_NOT_WORKER_OUTPUT",
    "inputScope": "Email plus PDF extracted separately; original ZIP not supplied in this diagnostic",
    "exception": failure["exception_type"],
    "message": failure["message"],
    "diagnosticFieldIssues": details["fieldIssues"],
    "fields": [],
    "requestContext": extraction.get("requestContext"),
    "requestNotes": extraction.get("specificationRequestNotes", []),
    "noteIssues": details.get("noteIssues"),
    "validatedRequestNotes": resolve_request_notes(extraction),
}
for label in PDF_FIELD_NAMES:
    p = provenance[label]
    summary["fields"].append({
        "field": label,
        "modelCandidate": "" if label == "Incoming Information" else extraction["performanceParameters"].get(label),
        "resolverCandidate": resolved[label],
        "resolverIssue": issues.get(label),
        "candidates": [dict(value=e["value"], role=e["role"], scope=e.get("scope"),
                            source_id=e["source_id"], snippet=e["snippet"])
                       for e in p.get("candidates", [])],
        "excluded": [dict(value=e["value"], role=e["role"], scope=e.get("scope"),
                          reason=e.get("reason")) for e in p.get("excludedCandidates", [])],
        "rejected": [dict(value=e["value"], reason=e.get("reason"))
                     for e in p.get("rejectedCandidates", [])],
    })
path = folder / "rejected_preview_summary.json"
path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"status": summary["status"], "requestContext": summary["requestContext"],
                  "fields": [{k: f[k] for k in ("field", "modelCandidate", "resolverCandidate", "resolverIssue")}
                             for f in summary["fields"]],
                  "validatedRequestNotes": summary["validatedRequestNotes"]}, indent=2, ensure_ascii=False))
