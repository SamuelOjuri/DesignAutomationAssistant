"""Check deployed source readers and same-Email-column ZIP intake synthetically.

Run on Render: python -m backend.scripts.check_design_processing_sources
No model requests, database access, or external writes are made.
"""

import io
import json
from zipfile import ZipFile

import openpyxl
from pypdf import PdfWriter

from backend.app.config import settings
from backend.app.services.design_processing_diagnostics import source_runtime_diagnostics
from backend.app.services.design_processing_inputs import EMAIL_COLUMN_ID, parse_design_processing_target
from backend.app.services.gemini_requests import runtime_diagnostics
from backend.app.services.legacy_enquiry.source_documents import extract_source_records


def check_email_column_zip_intake():
    """Exercise Monday file selection and revision identity without remote reads."""
    def snapshot(zip_size=None):
        assets = [{
            "id": "1", "name": "request.eml", "file_extension": "eml", "file_size": 20,
            "created_at": "2026-11-11T10:00:00Z", "public_url": "https://example.invalid/synthetic/1",
        }]
        members = [{"assetId": "1", "name": "request.eml"}]
        if zip_size is not None:
            assets.append({
                "id": "2", "name": "roof-plans.zip", "file_extension": "zip", "file_size": zip_size,
                "created_at": "2026-11-11T10:00:00Z", "public_url": "https://example.invalid/synthetic/2",
            })
            members.append({"assetId": "2", "name": "roof-plans.zip"})
        columns = [{"id": EMAIL_COLUMN_ID, "value": json.dumps({"files": members})}]
        columns.extend({"id": column_id, "value": json.dumps({"files": []})}
                       for column_id in settings.design_processing_source_file_column_ids
                       if column_id != EMAIL_COLUMN_ID)
        return parse_design_processing_target({
            "id": "1", "name": "Synthetic source intake", "state": "active",
            "board": {"id": "1882196103"}, "group": {"id": "diagnostic-source-intake"},
            "assets": assets, "column_values": columns,
        })

    email_only, with_zip, changed_zip = snapshot(), snapshot(100), snapshot(101)
    emails = [asset.filename for asset in with_zip.email_assets]
    supplementary = [asset.filename for asset in with_zip.supplementary_assets]
    zip_add_changes_revision = bool(with_zip.input_revision and email_only.input_revision != with_zip.input_revision)
    zip_change_changes_revision = bool(changed_zip.input_revision and with_zip.input_revision != changed_zip.input_revision)
    return {
        "source": "Email column: request.eml + roof-plans.zip",
        "passed": emails == ["request.eml"] and supplementary == ["roof-plans.zip"]
                  and zip_add_changes_revision and zip_change_changes_revision,
        "email_assets": emails, "supplementary_assets": supplementary,
        "zip_add_changes_revision": zip_add_changes_revision,
        "zip_change_changes_revision": zip_change_changes_revision,
    }


def check_sources():
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("request.txt", "Scale: 1:100")
        archive.writestr("drawing.dwg", b"CAD")
        archive.writestr("model.rvt", b"unsupported BIM format")
    archive_content = output.getvalue()
    output = io.BytesIO()
    book = openpyxl.Workbook()
    book.active.append(["Synthetic specification", "Description " * 200 + "Target U-value: 0.25"])
    book.save(output)
    book.close()
    workbook_content = output.getvalue()
    output = io.BytesIO()
    with PdfWriter() as writer:
        writer.add_blank_page(width=72, height=72)
        writer.encrypt("synthetic-password", algorithm="AES-256")
        writer.write(output)
    encrypted_pdf_content = output.getvalue()
    results = []
    for name, content, expected_statuses, required_text in (
        ("drawing.dwg", b"CAD", ["skipped:dwg"], ""),
        ("request.zip", archive_content,
         ["expanded:archive", "read", "skipped:dwg", "skipped:unsupported_format"], "Scale: 1:100"),
        ("long-cell.xlsx", workbook_content, ["read"], "Target U-value: 0.25"),
        ("protected.pdf", encrypted_pdf_content, ["skipped:encrypted_pdf"], ""),
        ("model.rvt", b"unsupported BIM format", ["skipped:unsupported_format"], ""),
        ("invalid-utf8.txt", b"\xff", ["failed:UnicodeDecodeError"], ""),
    ):
        records = extract_source_records(content, name, None, source_id="diagnostic")
        statuses = [record["status"] for record in records]
        text = "\n".join(record["text"] for record in records)
        results.append({"source": name, "passed": statuses == expected_statuses and required_text in text,
                        "source_statuses": statuses})
    results.append(check_email_column_zip_intake())
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
