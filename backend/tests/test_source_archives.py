import hashlib
import io
from email.message import EmailMessage
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from backend.app.services.legacy_enquiry import source_documents as sources
from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.email_extraction import extract_text_from_email
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.performance_specification_contract import FIELD_NAMES
from backend.app.services.performance_specification_evidence import resolve_email_evidence
from backend.app.services.specification_semantics import COMPLETE_SOURCE_STATUSES, EVIDENCE_VERSION
from backend.tests.pdf_helpers import pdf_bytes


def archive_bytes(entries, compression=ZIP_DEFLATED):
    output = io.BytesIO()
    with ZipFile(output, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def downloaded(tmp_path, filename, content, asset_id):
    path = tmp_path / filename
    path.write_bytes(content)
    return SimpleNamespace(
        temp_path=str(path), content_sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content),
        source=SimpleNamespace(asset_id=asset_id, filename=filename),
    )


def parameters():
    return DesignParameterExtraction(**{
        name: None for name in DesignParameterExtraction.model_fields
        if not name.startswith("specification_")
    })


def test_dwg_is_skipped_and_audited_without_entering_model_context():
    manifest = []
    context = extract_text_from_email(
        "Contact: Alex", [{"filename": "roof.DWG", "content": b"CAD PRIVATE TEXT"},
                          {"filename": "request.txt", "content": b"Scale: 1:100"}],
        extractor=SimpleNamespace(), manifest=manifest,
    )
    record = manifest[1]
    assert record["status"] == "skipped:dwg" and record["text"] == ""
    assert record["contentSha256"] == hashlib.sha256(b"CAD PRIVATE TEXT").hexdigest()
    assert "CAD PRIVATE TEXT" not in context and "roof.DWG" not in context
    assert "Scale: 1:100" in context and "Contact: Alex" in context


@pytest.mark.parametrize("embedded", [True, False])
def test_zip_and_nested_zip_documents_reach_analysis_with_individual_evidence(tmp_path, embedded):
    pdf = pdf_bytes()
    nested = archive_bytes([("roof.pdf", pdf), ("roof.DWG", b"CAD")])
    content = archive_bytes([("drawings/", b""), ("drawings/nested.zip", nested),
                             ("request.txt", b"Membrane: Single Ply")])
    mail = EmailMessage()
    mail["From"], mail["Subject"] = "client@example.com", "Roof request"
    mail.set_content("Contact: Alex")
    if embedded:
        mail.add_attachment(content, maintype="application", subtype="zip", filename="request.ZIP")
    assets = [downloaded(tmp_path, "request.eml", mail.as_bytes(), "1")]
    if not embedded:
        assets.append(downloaded(tmp_path, "request.ZIP", content, "2"))
        assets.append(downloaded(tmp_path, "supplementary.dwg", b"CAD", "3"))
    contexts, pdf_calls = [], []
    result = analyze_downloaded_email_assets(assets, client=SimpleNamespace(
        process_pdf=lambda content, filename: pdf_calls.append(filename) or "Scale: 1:100",
        extract_design_parameters=lambda context: contexts.append(context) or parameters(),
        query_llm=lambda *args: "Roof",
    ))
    manifest = list(result.source_manifest)
    assert all(record["status"] in COMPLETE_SOURCE_STATUSES for record in manifest)
    assert len({record["sourceId"] for record in manifest}) == len(manifest)
    assert sum(record["status"] == "expanded:archive" for record in manifest) == 2
    assert len(pdf_calls) == 1
    assert "Scale: 1:100" in contexts[0] and "Membrane: Single Ply" in contexts[0]
    assert "roof.DWG" not in contexts[0] and "supplementary.dwg" not in contexts[0]
    pdf_record = next(record for record in manifest if record["filename"].endswith("roof.pdf"))
    assert pdf_record["contentSha256"] == hashlib.sha256(pdf).hexdigest()
    assert pdf_record["parentSourceId"] in {record["sourceId"] for record in manifest}
    evidence = {"field": "Scale", "value": "1:100", "source_id": pdf_record["sourceId"],
                "snippet": "Scale: 1:100", "location": "page 1", "scope": None, "issuer": None,
                "role": "requested", "rationale": "Drawing scale"}
    extraction = {"specificationEvidenceVersion": EVIDENCE_VERSION,
                  "specificationAssessedFields": list(FIELD_NAMES), "performanceParameters": {},
                  "sourceManifest": manifest, "specificationEvidence": [evidence]}
    raw, _, issues = resolve_email_evidence(extraction)
    assert raw["Scale"] == "1:100" and not issues
    for status in ("skipped:dwg", "expanded:archive"):
        evidence["source_id"] = next(record["sourceId"] for record in manifest if record["status"] == status)
        assert "Scale" in resolve_email_evidence(extraction)[2]


def test_failed_document_inside_zip_is_not_hidden_by_skipped_dwg():
    content = archive_bytes([("roof.dwg", b"CAD"), ("roof.pdf", pdf_bytes())])
    def fail(*args):
        raise TimeoutError("secret signed URL")
    manifest = []
    with pytest.raises(sources.IncompleteSourceExtraction) as captured:
        extract_text_from_email("Request", [{"filename": "request.zip", "content": content}],
                                extractor=SimpleNamespace(process_pdf=fail), manifest=manifest)
    assert manifest[1]["status"] == "expanded:archive"
    assert manifest[2]["status"] == "skipped:dwg"
    assert manifest[3]["status"] == "failed:TimeoutError"
    assert "request.zip!/roof.pdf" in str(captured.value)
    assert "secret signed URL" not in str(captured.value)


@pytest.mark.parametrize("limit,maximum,entries,code", [
    ("MAX_SOURCE_BYTES", 10, [("a.txt", b"a")], "source_size_limit"),
    ("MAX_SOURCE_BYTES", 512, [("a.txt", b"a" * 1000)], "archive_member_size_limit"),
    ("MAX_ARCHIVE_EXPANDED_BYTES", 100, [("a.txt", b"a" * 60), ("b.txt", b"b" * 60)], "archive_expansion_limit"),
    ("MAX_ARCHIVE_MEMBERS", 1, [("a.txt", b"a"), ("b.txt", b"b")], "archive_member_limit"),
])
def test_archive_limits_block_before_reading_documents(monkeypatch, limit, maximum, entries, code):
    content = archive_bytes(entries)
    monkeypatch.setattr(sources, limit, maximum)
    def unexpected(*args):
        pytest.fail("Document reader should not run for an oversized archive")
    monkeypatch.setattr(sources, "extract_document", unexpected)
    records = sources.extract_source_records(content, "request.zip", None, source_id="zip:1")
    assert len(records) == 1 and records[0]["status"] == "failed:SourceDocumentError"
    assert records[0]["error"]["code"] == code


@pytest.mark.parametrize("limit,maximum,code", [
    ("MAX_ARCHIVE_MEMBERS", 2, "archive_member_limit"),
    ("MAX_ARCHIVE_DEPTH", 1, "archive_depth_limit"),
    ("MAX_ARCHIVE_EXPANDED_BYTES", 180, "archive_expansion_limit"),
])
def test_nested_archives_share_limits(monkeypatch, limit, maximum, code):
    inner = archive_bytes([("a.txt", b"a" * 100)])
    content = archive_bytes([("nested.zip", inner), ("b.txt", b"b")])
    monkeypatch.setattr(sources, limit, maximum)
    records = sources.extract_source_records(content, "request.zip", None, source_id="zip:1")
    assert any(record.get("error", {}).get("code") == code for record in records)
    assert any(record["status"] not in COMPLETE_SOURCE_STATUSES for record in records)


def test_zip_text_budget_stops_after_limit(monkeypatch):
    monkeypatch.setattr(sources, "MAX_CONTEXT_CHARS", 10)
    content = archive_bytes([("a.txt", b"a" * 6), ("b.txt", b"b" * 6), ("c.txt", b"c" * 6)])
    records = sources.extract_source_records(content, "request.zip", None, source_id="zip:1")
    assert records[0]["error"]["code"] == "archive_text_limit"
    assert len(records) == 3


@pytest.mark.parametrize("kind", ["invalid", "empty", "crc", "encrypted"])
def test_unreadable_archives_have_actionable_errors(kind):
    content = archive_bytes([("request.txt", b"Original content")], compression=ZIP_STORED)
    if kind == "invalid":
        content = b"not a ZIP"
    elif kind == "empty":
        content = archive_bytes([])
    elif kind == "crc":
        content = content.replace(b"Original content", b"Modified content")
    elif kind == "encrypted":
        patched = bytearray(content)
        patched[6] |= 1  # local file-header flags
        patched[patched.index(b"PK\x01\x02") + 8] |= 1  # central directory flags
        content = bytes(patched)
    records = sources.extract_source_records(content, "request.zip", None, source_id="zip:1")
    assert records[0]["status"] == "failed:SourceDocumentError"
    assert records[0]["error"]["code"] == {
        "invalid": "unreadable_archive", "empty": "empty_archive", "crc": "unreadable_archive",
        "encrypted": "encrypted_archive",
    }[kind]


def test_duplicate_and_relative_member_paths_keep_distinct_identities_without_disk_extraction(monkeypatch):
    with pytest.warns(UserWarning, match="Duplicate name"):
        content = archive_bytes([("../request.txt", b"Scale: 1:100"), ("../request.txt", b"Scale: 1:200")])
    def unexpected(*args, **kwargs):
        pytest.fail("ZIP paths must not be extracted to disk")
    monkeypatch.setattr(ZipFile, "extract", unexpected)
    monkeypatch.setattr(ZipFile, "extractall", unexpected)
    records = sources.extract_source_records(content, "request.zip", None, source_id="zip:1")
    assert [record["text"] for record in records[1:]] == ["Scale: 1:100", "Scale: 1:200"]
    assert records[1]["sourceId"] != records[2]["sourceId"]
    assert records[1]["contentSha256"] != records[2]["contentSha256"]
