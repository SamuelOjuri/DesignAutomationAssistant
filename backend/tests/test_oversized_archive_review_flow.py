"""Oversized ZIPs become explicit source-review work, never evidence or silent success."""

from copy import deepcopy
from email.message import EmailMessage
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_STORED, ZipFile

import pytest

from backend.app.models import PerformanceSpecificationArtifact
from backend.app.services import specification_review as service
from backend.app.services.legacy_enquiry import analysis as legacy_analysis
from backend.app.services.legacy_enquiry import source_documents
from backend.app.services.performance_specification_contract import SOURCE_REVIEW_DETAILS, source_review_notes
from backend.app.services.performance_specification_sources import resolve_specification_sources
from backend.app.services.specification_review_contract import ReviewSourceSnapshot
from backend.app.services.specification_semantics import OVERSIZED_ARCHIVE_REVIEW_STATUS
from backend.tests.test_performance_specification import BOARD, ITEM, case, current, pdf_text, run
from backend.tests.test_specification_publication_hardening import parameters
from backend.tests.test_specification_review_persistence import (
    ACCOUNT, ACTOR, FIELD_NAMES, approve, persisted, publish_current_review,
    review_db, save, save_command, source_snapshot,
)


ARCHIVE_ID = "asset:oversized-zip"
ARCHIVE_NAME = "roof-plans-and-photos.zip"


def oversized_archive():
    return {
        "sourceId": ARCHIVE_ID, "filename": ARCHIVE_NAME, "sourceType": "attachment",
        "status": OVERSIZED_ARCHIVE_REVIEW_STATUS, "text": "", "contentSha256": "b" * 64,
        "sizeBytes": 41 * 1024 * 1024, "observedAt": "2026-11-12T10:15:00+00:00",
    }


def add_archive(case):
    extraction = deepcopy(case.item.extracted_parameters_json)
    extraction["sourceManifest"].append(oversized_archive())
    case.item.extracted_parameters_json = extraction
    case.db.commit()


def test_oversized_zip_is_an_actionable_generic_review_source_issue(case):
    add_archive(case)
    resolved = resolve_specification_sources(case.gateway, case.item)
    snapshot = ReviewSourceSnapshot.model_validate(resolved["reviewSource"])
    issue, = snapshot.source_issues
    assert issue.source_id == ARCHIVE_ID and issue.code == OVERSIZED_ARCHIVE_REVIEW_STATUS
    assert ARCHIVE_NAME in issue.detail and "40 MiB" in issue.detail
    assert "Download and review the original ZIP manually" in issue.detail
    assert "contents were not used" in issue.detail
    assert source_review_notes(resolved["sourceManifest"]) == [issue.detail]
    # Existing populated facts still come only from the readable email evidence.
    assert resolved["raw"]["Scale"] == "1 : 100"
    assert not any(citation.source_id == ARCHIVE_ID
                   for field in snapshot.fields.values() for citation in field.proposal.email_evidence)


def test_oversized_zip_worker_succeeds_with_review_and_pdf_warning(case):
    add_archive(case)
    assert run(case) == ["published"]
    record = current(case)
    assert record.last_error is None and record.review_json["status"] == "published_with_review"
    review_note, = record.review_json["sourceReviews"]
    assert review_note.startswith(ARCHIVE_NAME + ": Not extracted.") and "40 MiB" in review_note
    artifact = case.db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, record.current_revision))
    assert artifact.translation_json["sourceReviews"] == [review_note]
    text = pdf_text(case.gateway.uploads[-1][1])
    assert "Review required: source information is missing or could not be read." in text
    assert ARCHIVE_NAME in text and "Download and review the original ZIP manually" in text


def test_oversized_zip_issue_prevents_completion_until_current_audited_disposition(review_db):
    payload = source_snapshot("oversized-zip-source").model_dump(mode="json")
    payload["source_issues"] = [{
        "source_id": ARCHIVE_ID, "code": OVERSIZED_ARCHIVE_REVIEW_STATUS,
        "detail": f"{ARCHIVE_NAME}: {SOURCE_REVIEW_DETAILS[OVERSIZED_ARCHIVE_REVIEW_STATUS]}",
    }]
    source = ReviewSourceSnapshot.model_validate(payload)
    service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
        source=source, expected_source_fingerprint="sources-1")
    save(review_db, save_command(fingerprint="oversized-zip-source",
        selections={"selected_request_type": "new_enquiry"}, field_operations={
            label: {"operation": "accept_recommendation", "proposal_id": source.fields[label].proposal.proposal_id}
            for label in FIELD_NAMES
        }), source=source)
    command = publish_current_review(review_db, source)
    with pytest.raises(service.ReviewConflict) as caught:
        approve(review_db, command, source)
    assert caught.value.code == "source_unavailable"
    assert persisted(review_db)["approval_id"] is None

    explanation = "Downloaded and reviewed the original roof plans and photographs; recorded the required corrections."
    save(review_db, save_command(version=command.expected_review_version, fingerprint="oversized-zip-source",
        source_dispositions=[{"source_id": ARCHIVE_ID, "issue_code": OVERSIZED_ARCHIVE_REVIEW_STATUS,
                              "explanation": explanation}]), source=source)
    disposition, = persisted(review_db)["dispositions"]
    assert disposition["explanation"] == explanation
    assert disposition["audit"]["reviewed_source"] == source.identity.model_dump(mode="json")
    assert disposition["audit"]["actor"] == ACTOR.model_dump(mode="json")
    current_command = publish_current_review(review_db, source)
    assert approve(review_db, current_command, source).approval_id is not None


def supplementary_downloads(tmp_path, filename, extension):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "client@example.com", "sales@taperedplus.co.uk", "Roof request"
    message["Date"] = "Tue, 10 Nov 2026 10:00:00 +0000"
    message.set_content("Contact: Alex\nPlease review the supplied roof package.")
    archive = io.BytesIO()
    with ZipFile(archive, "w", compression=ZIP_STORED) as package:
        package.writestr("roof.txt", b"Unread archive content" * 100)
    content = {"request.eml": message.as_bytes(), filename: archive.getvalue()}
    downloads = []
    for asset_id, (name, value) in enumerate(content.items(), start=1):
        path = tmp_path / name
        path.write_bytes(value)
        downloads.append(SimpleNamespace(
            temp_path=str(path), content_sha256=hashlib.sha256(value).hexdigest(), size_bytes=len(value),
            source=SimpleNamespace(asset_id=str(asset_id), filename=name,
                                   file_extension=extension if name == filename else ".eml"),
        ))
    return downloads


@pytest.mark.parametrize("filename,extension", [("large.ZIP", None), ("downloaded-roof-package", ".ZIP")])
def test_oversized_supplementary_zip_avoids_full_byte_load_and_reports_review_progress(
        tmp_path, monkeypatch, filename, extension):
    downloads = supplementary_downloads(tmp_path, filename, extension)
    archive_path = Path(downloads[1].temp_path)
    monkeypatch.setattr(source_documents, "MAX_SOURCE_BYTES", 1024)
    read_bytes = Path.read_bytes

    def read_allowed_file(path):
        assert path != archive_path, "Oversized ZIP must never be loaded as one bytes object"
        return read_bytes(path)

    def unexpected_extraction(*args, **kwargs):
        pytest.fail("Oversized supplementary ZIP must not enter document extraction")

    monkeypatch.setattr(Path, "read_bytes", read_allowed_file)
    monkeypatch.setattr(legacy_analysis, "extract_source_records", unexpected_extraction)
    contexts, progress = [], []
    result = legacy_analysis.analyze_downloaded_email_assets(downloads, client=SimpleNamespace(
        extract_design_parameters=lambda context: contexts.append(context) or parameters(),
        query_llm=lambda *args: "Roof request",
    ), on_progress=progress.append)
    archive, = [record for record in result.source_manifest if record["sourceId"] == "asset:2"]
    assert archive["status"] == OVERSIZED_ARCHIVE_REVIEW_STATUS and archive["text"] == ""
    assert archive["contentSha256"] == downloads[1].content_sha256
    assert archive["sizeBytes"] == downloads[1].size_bytes
    assert "Unread archive content" not in contexts[0] and "Contact: Alex" in contexts[0]
    assert any(event["source_counts"]["review"] == 1 for event in progress)
    assert all(event["source_counts"]["failed"] == 0 for event in progress)
    archive_path.unlink()  # The streamed preflight closes its handle on Windows.


@pytest.mark.parametrize("mismatch", ["hash", "size"])
def test_oversized_zip_preflight_still_enforces_download_identity(tmp_path, monkeypatch, mismatch):
    downloads = supplementary_downloads(tmp_path, "large.zip", ".zip")
    monkeypatch.setattr(source_documents, "MAX_SOURCE_BYTES", 1024)
    if mismatch == "hash":
        downloads[1].content_sha256 = "a" * 64
    else:
        downloads[1].size_bytes += 1
    with pytest.raises(ValueError, match="Downloaded source changed before transcription"):
        legacy_analysis.analyze_downloaded_email_assets(downloads, client=SimpleNamespace(
            extract_design_parameters=lambda context: pytest.fail("Changed source cannot reach extraction"),
            query_llm=lambda *args: "Roof request",
        ))
