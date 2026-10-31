"""Encrypted sources are audited exclusions, never model input or evidence."""

import hashlib
import io
from email.message import EmailMessage
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.email_extraction import extract_text_from_email
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.legacy_enquiry.source_documents import IncompleteSourceExtraction, extract_source_records
from backend.app.services.performance_specification_contract import FIELD_NAMES
from backend.app.services.performance_specification_evidence import resolve_email_evidence
from backend.app.services.specification_semantics import COMPLETE_SOURCE_STATUSES, EVIDENCE_VERSION
from backend.tests.pdf_helpers import pdf_bytes


def zip_bytes(entries):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for filename, content in entries:
            archive.writestr(filename, content)
    return output.getvalue()


@pytest.mark.parametrize("algorithm,password", [
    ("RC4-128", "locked"), ("AES-128", "locked"), ("AES-256", "locked"), ("AES-256", ""),
])
def test_encrypted_pdf_is_skipped_before_model_call_and_keeps_original_identity(algorithm, password):
    content = pdf_bytes(encrypted=True, user_password=password, algorithm=algorithm)

    def unexpected(*args):
        pytest.fail("Encrypted PDF bytes must never be sent to the model")

    records = extract_source_records(
        content, "purchase-order.PDF", SimpleNamespace(process_pdf=unexpected),
        source_id="asset:1:attachment:0", observed_at="2026-10-31T00:00:00+00:00",
    )
    assert records == [{
        "sourceId": "asset:1:attachment:0", "filename": "purchase-order.PDF",
        "sourceType": "attachment", "contentSha256": hashlib.sha256(content).hexdigest(),
        "sizeBytes": len(content), "observedAt": "2026-10-31T00:00:00+00:00",
        "status": "skipped:encrypted_pdf", "text": "",
    }]


def test_unencrypted_pdf_is_still_sent_to_the_model_unchanged():
    content, calls = pdf_bytes(), []
    records = extract_source_records(content, "roof.pdf", SimpleNamespace(
        process_pdf=lambda *args: calls.append(args) or "Scale: 1:100",
    ), source_id="asset:1")
    assert calls == [(content, "roof.pdf")]
    assert records[0]["status"] == "read" and records[0]["text"] == "Scale: 1:100"


@pytest.mark.parametrize("route", ["email", "supplementary", "nested_zip"])
def test_encrypted_sources_allow_analysis_and_publication_evidence_from_readable_sources(tmp_path, route):
    protected = pdf_bytes(encrypted=True)
    plain = pdf_bytes()
    mail = EmailMessage()
    mail["From"], mail["Subject"] = "client@example.com", "Roof request"
    mail["Date"] = "Sat, 31 Oct 2026 00:00:00 +0000"
    mail.set_content("Contact: Alex")
    if route == "email":
        mail.add_attachment(protected, maintype="application", subtype="pdf", filename="protected.pdf")
        mail.add_attachment(plain, maintype="application", subtype="pdf", filename="roof.pdf")
    elif route == "nested_zip":
        nested = zip_bytes([("protected.pdf", protected), ("roof.pdf", plain)])
        mail.add_attachment(zip_bytes([("nested.zip", nested)]),
                            maintype="application", subtype="zip", filename="request.zip")
    inputs = [("request.eml", mail.as_bytes())]
    if route == "supplementary":
        inputs += [("protected.pdf", protected), ("roof.pdf", plain)]
    downloaded = []
    for index, (filename, content) in enumerate(inputs):
        path = tmp_path / filename
        path.write_bytes(content)
        downloaded.append(SimpleNamespace(
            temp_path=str(path), content_sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content),
            source=SimpleNamespace(asset_id=str(index + 1), filename=filename),
        ))
    calls, contexts = [], []

    def process_pdf(content, filename):
        assert content == plain
        calls.append(filename)
        return "Scale: 1:100"

    parameters = DesignParameterExtraction(**{
        name: None for name in DesignParameterExtraction.model_fields
        if name not in {"specification_evidence", "specification_assessed_fields"}
    })
    result = analyze_downloaded_email_assets(downloaded, client=SimpleNamespace(
        process_pdf=process_pdf,
        extract_design_parameters=lambda context: contexts.append(context) or parameters,
        query_llm=lambda *args: "Roof",
    ))
    assert len(calls) == 1 and calls[0].endswith("roof.pdf")
    assert "Scale: 1:100" in contexts[0] and "Contact: Alex" in contexts[0]
    assert "protected.pdf" not in contexts[0]
    manifest = list(result.source_manifest)
    assert all(source["status"] in COMPLETE_SOURCE_STATUSES for source in manifest)
    skipped = next(source for source in manifest if source["status"] == "skipped:encrypted_pdf")
    assert skipped["text"] == "" and skipped["contentSha256"] == hashlib.sha256(protected).hexdigest()
    if route == "nested_zip":
        assert skipped["parentSourceId"] in {source["sourceId"] for source in manifest}
    readable = next(source for source in manifest if source["filename"].endswith("roof.pdf"))
    evidence = {
        "field": "Scale", "value": "1:100", "source_id": readable["sourceId"],
        "snippet": "Scale: 1:100", "location": "page 1", "scope": None, "issuer": None,
        "role": "requested", "rationale": "Drawing scale",
    }
    extraction = {
        "specificationEvidenceVersion": EVIDENCE_VERSION, "specificationAssessedFields": list(FIELD_NAMES),
        "performanceParameters": {}, "sourceManifest": manifest, "specificationEvidence": [evidence],
    }
    raw, _, issues = resolve_email_evidence(extraction)
    assert raw["Scale"] == "1:100" and not issues
    # Even a forged excerpt must not promote a skipped source to evidence.
    skipped["text"] = "Scale: 1:100"
    evidence["source_id"] = skipped["sourceId"]
    raw, _, issues = resolve_email_evidence(extraction)
    assert raw["Scale"] is None and "Scale" in issues


def test_skipped_encrypted_pdf_does_not_hide_a_real_source_failure():
    def fail(*args):
        raise TimeoutError("private provider response")

    manifest = []
    with pytest.raises(IncompleteSourceExtraction) as captured:
        extract_text_from_email("Contact: Alex", [
            {"filename": "protected.pdf", "content": pdf_bytes(encrypted=True)},
            {"filename": "roof.pdf", "content": pdf_bytes()},
        ], extractor=SimpleNamespace(process_pdf=fail), manifest=manifest)
    assert [source["status"] for source in manifest] == ["read", "skipped:encrypted_pdf", "failed:TimeoutError"]
    assert "roof.pdf" in str(captured.value) and "protected.pdf" not in str(captured.value)
    assert "private provider response" not in str(captured.value)


def test_malformed_pdf_requires_review_instead_of_being_silently_skipped():
    def unexpected(*args):
        pytest.fail("A malformed PDF should fail before a model call")

    records = extract_source_records(b"%PDF-1.4\ntruncated", "broken.pdf",
                                    SimpleNamespace(process_pdf=unexpected), source_id="asset:1")
    assert records[0]["status"] == "failed:SourceDocumentError"
    assert records[0]["error"]["code"] == "unreadable_pdf" and records[0]["text"] == ""
