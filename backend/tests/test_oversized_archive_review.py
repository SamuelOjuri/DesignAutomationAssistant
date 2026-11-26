"""Oversized ZIPs require explicit review without claiming their contents were read."""

import hashlib
import io
import logging
import random
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from backend.app.services.legacy_enquiry import source_documents as sources
from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.email_extraction import extract_text_from_email
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.performance_specification_contract import FIELD_NAMES
from backend.app.services.performance_specification_evidence import resolve_email_evidence
from backend.app.services.specification_semantics import (
    COMPLETE_SOURCE_STATUSES, EVIDENCE_VERSION, REVIEW_SOURCE_STATUSES,
)


REVIEW_STATUS = "review:oversized_archive"


def archive_bytes(entries, compression=ZIP_DEFLATED):
    output = io.BytesIO()
    with ZipFile(output, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def parameters():
    return DesignParameterExtraction(**{
        name: None for name in DesignParameterExtraction.model_fields
        if not name.startswith("specification_")
    })


def downloaded(tmp_path, filename, content, asset_id):
    path = tmp_path / filename
    path.write_bytes(content)
    return SimpleNamespace(
        temp_path=str(path), content_sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content),
        source=SimpleNamespace(asset_id=asset_id, filename=filename),
    )


def test_zip_automatic_processing_size_limit_remains_forty_mib():
    assert sources.MAX_SOURCE_BYTES == 40 * 1024 * 1024


@pytest.mark.parametrize("filename,extension", [
    ("large-drawings.zip", None), ("large-drawings.ZIP", None), ("large-download", ".ZIP"),
])
def test_oversized_zip_is_reviewable_without_opening_archive_or_calling_document_reader(
    monkeypatch, caplog, filename, extension,
):
    content = archive_bytes([("private.txt", b"PRIVATE UNREAD ARCHIVE CONTENT")])
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", len(content) - 1)

    def unexpected(*args, **kwargs):
        pytest.fail("An oversized archive must not be opened or sent for document extraction")

    monkeypatch.setattr(sources, "ZipFile", unexpected)
    monkeypatch.setattr(sources, "extract_document", unexpected)
    progress = []
    with caplog.at_level(logging.INFO, logger=sources.__name__):
        records = sources.extract_source_records(
            content, filename, object(), source_id="asset:2", file_extension=extension, on_source=progress.append,
        )

    record, = records
    assert record["status"] == REVIEW_STATUS and record["status"] in REVIEW_SOURCE_STATUSES
    assert record["status"] in COMPLETE_SOURCE_STATUSES and record["text"] == ""
    assert record["filename"] == filename and record["sizeBytes"] == len(content)
    assert record["contentSha256"] == hashlib.sha256(content).hexdigest()
    assert record["error"]["stage"] == "source_document"
    assert record["error"]["code"] == "source_size_limit"
    assert progress == records
    # A referral is not a source failure; only an inventory with nothing read is permanent.
    assert not sources.permanent_source_failure([{"sourceId": "asset:1:body", "status": "read"}, *records])
    assert sources.permanent_source_failure(records)
    log_records = [entry for entry in caplog.records if entry.name == sources.__name__]
    assert any(entry.levelno == logging.INFO for entry in log_records)
    assert all(entry.levelno < logging.WARNING for entry in log_records)


def test_zip_exactly_at_processing_limit_is_still_extracted(monkeypatch):
    content = archive_bytes([("request.txt", b"Scale: 1:100")])
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", len(content))
    records = sources.extract_source_records(content, "plans.zip", None, source_id="zip:1")
    assert [record["status"] for record in records] == ["expanded:archive", "read"]
    assert records[1]["text"] == "Scale: 1:100"


def test_unread_oversized_zip_cannot_supply_field_evidence(monkeypatch):
    content = archive_bytes([("request.txt", b"Scale: 1:500")])
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", len(content) - 1)
    manifest = []
    context = extract_text_from_email(
        "Contact: Alex", [{"filename": "plans.zip", "content": content}],
        extractor=SimpleNamespace(), manifest=manifest, source_prefix="mail",
    )
    assert manifest[1]["status"] == REVIEW_STATUS
    assert "Contact: Alex" in context and "Scale: 1:500" not in context
    extraction = {
        "specificationEvidenceVersion": EVIDENCE_VERSION,
        "specificationAssessedFields": list(FIELD_NAMES), "performanceParameters": {},
        "sourceManifest": manifest, "specificationEvidence": [{
            "field": "Scale", "value": "1:500", "source_id": "mail:attachment:0",
            "snippet": "Scale: 1:500", "location": "unread ZIP", "scope": None,
            "issuer": None, "role": "requested", "rationale": "Must not trust unopened archive contents",
        }],
    }
    raw, _, issues = resolve_email_evidence(extraction)
    assert raw["Scale"] is None and "Scale" in issues


@pytest.mark.parametrize("embedded", [True, False])
def test_oversized_zip_allows_other_sources_to_reach_analysis(monkeypatch, tmp_path, embedded):
    content = archive_bytes([("private.txt", b"PRIVATE UNREAD ARCHIVE CONTENT")])
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", len(content) - 1)
    mail = EmailMessage()
    mail["From"], mail["To"], mail["Subject"] = "client@example.com", "sales@taperedplus.co.uk", "Roof request"
    mail["Date"] = "Tue, 10 Nov 2026 10:00:00 +0000"
    mail.set_content("Contact: Alex. Please price the roof.")
    if embedded:
        mail.add_attachment(content, maintype="application", subtype="zip", filename="large-plans.ZIP")
    assets = [downloaded(tmp_path, "request.eml", mail.as_bytes(), "1")]
    if not embedded:
        assets.append(downloaded(tmp_path, "large-plans.ZIP", content, "2"))
    contexts, progress = [], []
    result = analyze_downloaded_email_assets(assets, client=SimpleNamespace(
        extract_design_parameters=lambda context: contexts.append(context) or parameters(),
        query_llm=lambda *args: "Roof",
    ), on_progress=progress.append)

    unread, = [record for record in result.source_manifest if record["filename"] == "large-plans.ZIP"]
    assert unread["status"] == REVIEW_STATUS and unread["text"] == ""
    assert unread["contentSha256"] == hashlib.sha256(content).hexdigest()
    assert all(record["status"] in COMPLETE_SOURCE_STATUSES for record in result.source_manifest)
    assert "Contact: Alex" in contexts[0] and "PRIVATE UNREAD ARCHIVE CONTENT" not in contexts[0]
    assert any(event["source_counts"]["review"] == 1 for event in progress)
    assert all(event["source_counts"]["failed"] == 0 for event in progress)


def request_email_bytes():
    mail = EmailMessage()
    mail["From"], mail["To"], mail["Subject"] = "client@example.com", "sales@taperedplus.co.uk", "Roof request"
    mail["Date"] = "Tue, 10 Nov 2026 10:00:00 +0000"
    mail.set_content("Contact: Alex. Please price the roof.")
    return mail.as_bytes()


@pytest.mark.parametrize("filename,extension", [("large-plans.ZIP", None), ("large-download", ".ZIP")])
def test_downloaded_oversized_zip_reaches_review_without_any_full_file_read(monkeypatch, tmp_path, filename, extension):
    content = archive_bytes([("private.txt", b"PRIVATE UNREAD ARCHIVE CONTENT")])
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", len(content) - 1)
    email = downloaded(tmp_path, "request.eml", request_email_bytes(), "1")
    archive = downloaded(tmp_path, filename, content, "2")
    archive.source.file_extension = extension
    read_bytes = Path.read_bytes
    email_reads = []

    def guarded_read(path):
        if path == Path(archive.temp_path):
            pytest.fail("The oversized ZIP must be hashed from its file stream, never read_bytes")
        email_reads.append(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    contexts = []
    result = analyze_downloaded_email_assets([email, archive], client=SimpleNamespace(
        extract_design_parameters=lambda context: contexts.append(context) or parameters(),
        query_llm=lambda *args: "Roof",
    ))
    record, = [source for source in result.source_manifest if source["sourceId"] == "asset:2"]
    assert record["status"] == REVIEW_STATUS and record["text"] == ""
    assert record["filename"] == filename and record["sizeBytes"] == len(content)
    assert record["contentSha256"] == hashlib.sha256(content).hexdigest()
    assert email_reads == [Path(email.temp_path)]
    assert "Contact: Alex" in contexts[0] and "PRIVATE UNREAD ARCHIVE CONTENT" not in contexts[0]


@pytest.mark.parametrize("changed_identity", ["hash", "size"])
def test_downloaded_oversized_zip_still_rejects_changed_download_identity(monkeypatch, tmp_path, changed_identity):
    content = archive_bytes([("private.txt", b"PRIVATE UNREAD ARCHIVE CONTENT")])
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", len(content) - 1)
    email = downloaded(tmp_path, "request.eml", request_email_bytes(), "1")
    archive = downloaded(tmp_path, "large-plans.zip", content, "2")
    if changed_identity == "hash":
        archive.content_sha256 = "0" * 64
    else:
        archive.size_bytes += 1
    read_bytes = Path.read_bytes

    def guarded_read(path):
        if path == Path(archive.temp_path):
            pytest.fail("Download identity validation must not require a full ZIP read")
        return read_bytes(path)

    def unexpected(*args, **kwargs):
        pytest.fail("A changed source identity must not reach the model")

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    with pytest.raises(ValueError, match="Downloaded source changed before transcription"):
        analyze_downloaded_email_assets([email, archive], client=SimpleNamespace(
            extract_design_parameters=unexpected, query_llm=unexpected,
        ))


def test_path_backed_archive_review_hashes_in_bounded_chunks(monkeypatch, tmp_path):
    content = archive_bytes([("private.txt", b"x" * (2 * 1024 * 1024 + 7))], compression=ZIP_STORED)
    path = tmp_path / "large-plans.zip"
    path.write_bytes(content)
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", 1024)
    original_open = Path.open
    read_sizes = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            read_sizes.append(size)
            return self.stream.read(size)

    def bounded_open(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)
        return BoundedReader(stream) if candidate == path else stream

    monkeypatch.setattr(Path, "open", bounded_open)
    record, = sources.archive_file_records(path, path.name, None, source_id="asset:2")
    assert record["status"] == REVIEW_STATUS and record["sizeBytes"] == len(content)
    assert record["contentSha256"] == hashlib.sha256(content).hexdigest()
    assert len(read_sizes) >= 3
    path.unlink()


def test_path_backed_small_zip_keeps_automatic_extraction(monkeypatch, tmp_path):
    content = archive_bytes([("request.txt", b"Scale: 1:100")])
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", len(content))
    email = downloaded(tmp_path, "request.eml", request_email_bytes(), "1")
    archive = downloaded(tmp_path, "plans.zip", content, "2")
    assert sources.archive_file_records(archive.temp_path, archive.source.filename, None, source_id="asset:2") is None
    contexts = []
    result = analyze_downloaded_email_assets([email, archive], client=SimpleNamespace(
        extract_design_parameters=lambda context: contexts.append(context) or parameters(),
        query_llm=lambda *args: "Roof",
    ))
    records = [source for source in result.source_manifest if source["sourceId"].startswith("asset:2")]
    assert [source["status"] for source in records] == ["expanded:archive", "read"]
    assert "Scale: 1:100" in contexts[0]


@pytest.mark.parametrize("slack,expanded", [(0, True), (-1, False)])
def test_downloaded_zip_over_limit_is_read_from_disk_when_only_mesh_exceeds_it(
        monkeypatch, tmp_path, slack, expanded):
    # Render job 823f5115 (item 3381414648): a 49.6 MiB TaperedView ZIP held 41.9 MiB of
    # compressed OBJ mesh, so nothing was read and every attempt failed.
    content = archive_bytes([("Scan_1/roof_scan.obj", random.Random(0).randbytes(4000)),
                             ("project_summary.txt", b"Target U-value: 0.15")])
    with ZipFile(io.BytesIO(content)) as archive:
        mesh_bytes = archive.getinfo("Scan_1/roof_scan.obj").compress_size
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", len(content) - mesh_bytes + slack)
    email = downloaded(tmp_path, "request.eml", request_email_bytes(), "1")
    archive = downloaded(tmp_path, "Site_TaperedView.zip", content, "2")
    read_bytes = Path.read_bytes

    def guarded_read(path):
        if path == Path(archive.temp_path):
            pytest.fail("A ZIP above the in-memory limit must be read from its file")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    contexts = []
    result = analyze_downloaded_email_assets([email, archive], client=SimpleNamespace(
        extract_design_parameters=lambda context: contexts.append(context) or parameters(),
        query_llm=lambda *args: "Roof",
    ))

    records = [source for source in result.source_manifest if source["sourceId"].startswith("asset:2")]
    assert records[0]["contentSha256"] == hashlib.sha256(content).hexdigest()
    assert records[0]["sizeBytes"] == len(content)
    if expanded:
        assert [source["status"] for source in records] == [
            "expanded:archive", "review:unsupported_archive_member", "read"]
        assert "Target U-value: 0.15" in contexts[0]
    else:
        assert [source["status"] for source in records] == [REVIEW_STATUS]
        assert "Target U-value: 0.15" not in contexts[0]


def test_inputs_without_any_readable_source_fail_permanently_before_model_requests(monkeypatch, tmp_path):
    # Render job 823f5115 (item 3381414648): the only input was an unread ZIP, yet the
    # model was called and three attempts failed with 'Source manifest is empty'.
    content = archive_bytes([("private.txt", b"PRIVATE UNREAD ARCHIVE CONTENT")])
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", len(content) - 1)

    def unexpected(*args, **kwargs):
        pytest.fail("No model request may be made without a readable source")

    with pytest.raises(sources.IncompleteSourceExtraction, match="No readable source") as error:
        analyze_downloaded_email_assets([downloaded(tmp_path, "plans.zip", content, "2")],
            client=SimpleNamespace(extract_design_parameters=unexpected, query_llm=unexpected))

    manifest = error.value.manifest
    assert [source["status"] for source in manifest] == [REVIEW_STATUS, "review:missing_client_email"]
    assert sources.permanent_source_failure(manifest)
    assert str(sources.IncompleteSourceExtraction(manifest)) == str(error.value)  # Survives the process boundary.


@pytest.mark.parametrize("filename", ["large.pdf", "large.txt", "large.png"])
def test_oversized_non_zip_documents_keep_processing_failure(monkeypatch, filename):
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", 10)
    record, = sources.extract_source_records(b"oversized document", filename, None, source_id="source:1")
    assert record["status"] == "failed:SourceDocumentError"
    assert record["error"]["code"] == "source_size_limit"
    assert record["status"] not in COMPLETE_SOURCE_STATUSES


@pytest.mark.parametrize("kind,code", [
    ("invalid", "unreadable_archive"), ("empty", "empty_archive"),
    ("crc", "unreadable_archive"), ("encrypted", "encrypted_archive"),
])
def test_other_archive_failures_remain_failures(kind, code):
    content = archive_bytes([("request.txt", b"Original content")], compression=ZIP_STORED)
    if kind == "invalid":
        content = b"not a ZIP"
    elif kind == "empty":
        content = archive_bytes([])
    elif kind == "crc":
        content = content.replace(b"Original content", b"Modified content")
    elif kind == "encrypted":
        patched = bytearray(content)
        patched[6] |= 1
        patched[patched.index(b"PK\x01\x02") + 8] |= 1
        content = bytes(patched)
    record, = sources.extract_source_records(content, "request.zip", None, source_id="source:1")
    assert record["status"] == "failed:SourceDocumentError"
    assert record["error"]["code"] == code


def test_archive_expansion_limit_remains_a_processing_failure(monkeypatch):
    content = archive_bytes([("a.txt", b"a" * 60), ("b.txt", b"b" * 60)])
    monkeypatch.setattr(sources, "MAX_ARCHIVE_EXPANDED_BYTES", 100)
    record, = sources.extract_source_records(content, "request.zip", None, source_id="source:1")
    assert record["status"] == "failed:SourceDocumentError"
    assert record["error"]["code"] == "archive_expansion_limit"


def test_archive_member_document_limit_remains_a_processing_failure(monkeypatch):
    content = archive_bytes([("large.txt", b"x" * 2000)])
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", 1024)
    record, = sources.extract_source_records(content, "request.zip", None, source_id="source:1")
    assert record["status"] == "failed:SourceDocumentError"
    assert record["error"]["code"] == "archive_member_size_limit"


@pytest.mark.parametrize("oversized_document", [False, True])
def test_nested_oversized_zip_is_reviewable_but_cannot_hide_an_oversized_document(monkeypatch, oversized_document):
    nested = archive_bytes([("private.txt", b"x" * 5000)], compression=ZIP_STORED)
    entries = [("nested.zip", nested)]
    if oversized_document:
        entries.append(("large.pdf", b"p" * 3000))
    content = archive_bytes(entries)
    assert len(content) <= 1024 < len(nested)
    monkeypatch.setattr(sources, "MAX_SOURCE_BYTES", 1024)

    def unexpected(*args, **kwargs):
        pytest.fail("An oversized nested archive must be classified before opening any member")

    monkeypatch.setattr(ZipFile, "open", unexpected)
    record, = sources.extract_source_records(content, "request.zip", None, source_id="source:1")
    assert record["status"] == ("failed:SourceDocumentError" if oversized_document else REVIEW_STATUS)
    assert record["error"]["code"] == "archive_member_size_limit"
    assert record["text"] == ""
