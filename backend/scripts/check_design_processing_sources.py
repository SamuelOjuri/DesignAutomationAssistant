"""Check deployed DWG/ZIP/workbook handling using synthetic files only.

Run on Render: python -m backend.scripts.check_design_processing_sources
No model requests, database access, or external writes are made.
"""

import io
import json
from zipfile import ZipFile

import openpyxl

from backend.app.config import settings
from backend.app.services.design_processing_diagnostics import source_runtime_diagnostics
from backend.app.services.gemini_requests import runtime_diagnostics
from backend.app.services.legacy_enquiry.source_documents import extract_source_records


def check_sources():
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("request.txt", "Scale: 1:100")
        archive.writestr("drawing.dwg", b"CAD")
    archive_content = output.getvalue()
    output = io.BytesIO()
    book = openpyxl.Workbook()
    book.active.append(["Synthetic specification", "Description " * 200 + "Target U-value: 0.25"])
    book.save(output)
    book.close()
    results = []
    for name, content, expected_statuses, required_text in (
        ("drawing.dwg", b"CAD", ["skipped:dwg"], ""),
        ("request.zip", archive_content, ["expanded:archive", "read", "skipped:dwg"], "Scale: 1:100"),
        ("long-cell.xlsx", output.getvalue(), ["read"], "Target U-value: 0.25"),
    ):
        records = extract_source_records(content, name, None, source_id="diagnostic")
        statuses = [record["status"] for record in records]
        text = "\n".join(record["text"] for record in records)
        results.append({"source": name, "passed": statuses == expected_statuses and required_text in text,
                        "source_statuses": statuses})
    return results


def main():
    checks = check_sources()
    succeeded = all(check["passed"] for check in checks)
    print(json.dumps({
        "event": "design_source_diagnostic", "status": "ok" if succeeded else "failed",
        **runtime_diagnostics(settings.design_processing_extraction_model, settings.design_processing_thinking_level),
        **source_runtime_diagnostics(), "checks": checks,
    }), flush=True)
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
