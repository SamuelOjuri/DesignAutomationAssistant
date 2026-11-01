"""Regression tests for the extraction gaps in the performance specification."""

import hashlib
from email.message import EmailMessage
import io
from types import SimpleNamespace
from zipfile import ZipFile

import openpyxl
import pytest

from backend.app.config import settings
from backend.app.services.design_processing_inputs import (
    DesignEmailAsset, DesignProcessingInputError, DownloadedDesignEmailAsset, parse_design_processing_target,
)
from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.email_extraction import extract_text_from_email, process_email_content
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.legacy_enquiry.source_documents import (
    AnimatedImageSkipped, IncompleteSourceExtraction, extract_document, extract_source_records,
)
from backend.app.services.performance_specification_contract import FIELD_NAMES
from backend.app.services.performance_specification_evidence import resolve_email_evidence
from backend.app.services.spreadsheet_extraction import extract_spreadsheet_documents
from backend.app.services.specification_semantics import PROVIDER_RECITATION_REVIEW_STATUS, SpecificationEvidence
from backend.tests.pdf_helpers import pdf_bytes


def parameters():
    return DesignParameterExtraction(**{name: None for name in DesignParameterExtraction.model_fields
                                       if not name.startswith("specification_")})


def image_bytes(format_name, *, animated):
    from PIL import Image, features
    if format_name == "WEBP" and not features.check("webp"):
        pytest.skip("Pillow was built without WebP support")
    output = io.BytesIO()
    first = Image.new("RGB", (4, 4), "red")
    options = {"save_all": True, "append_images": [Image.new("RGB", (4, 4), "blue")],
               "duration": 100, "loop": 0} if animated else {}
    first.save(output, format=format_name, **options)
    return output.getvalue()


@pytest.mark.parametrize("format_name,suffix", [("GIF", "gif"), ("PNG", "png"), ("WEBP", "webp")])
def test_animated_images_are_skipped_before_calling_the_model(format_name, suffix):
    calls = []
    client = SimpleNamespace(process_image=lambda *args: calls.append(args) or "unwanted text")
    with pytest.raises(AnimatedImageSkipped):
        extract_document(image_bytes(format_name, animated=True), "signature." + suffix, client)
    assert not calls


@pytest.mark.parametrize("format_name,suffix,expected_suffix", [("GIF", "gif", "gif.png"), ("PNG", "png", "png"),
                                                              ("WEBP", "webp", "webp"), ("BMP", "bmp", "bmp.png")])
def test_static_images_still_extract_information(format_name, suffix, expected_suffix):
    calls = []
    client = SimpleNamespace(process_image=lambda *args: calls.append(args) or "Scale: 1:100")
    assert extract_document(image_bytes(format_name, animated=False), "drawing." + suffix, client) == "Scale: 1:100"
    assert len(calls) == 1 and calls[0][1] == "drawing." + expected_suffix


@pytest.mark.parametrize("inline", [False, True])
def test_skipped_animation_is_audited_without_blocking_other_email_evidence(inline):
    manifest, calls = [], []
    client = SimpleNamespace(max_attachment_workers=2,
                             process_image=lambda *args: calls.append(args) or "unwanted text",
                             process_pdf=lambda *args: "Scale: 1:100")
    animation = {"filename": "signature.gif", "content": image_bytes("GIF", animated=True)}
    context = extract_text_from_email(
        "Contact: Alex", [{"filename": "roof.pdf", "content": pdf_bytes()}] + ([] if inline else [animation]),
        inline_images=[animation] if inline else [], extractor=client, manifest=manifest, source_prefix="mail",
    )
    assert "Contact: Alex" in context and "Scale: 1:100" in context
    assert "signature.gif" not in context and not calls
    skipped = next(source for source in manifest if source["filename"] == "signature.gif")
    assert skipped["status"] == "skipped:animated_image" and skipped["text"] == ""
    assert skipped["contentSha256"] == hashlib.sha256(animation["content"]).hexdigest()
    extraction = {"specificationEvidenceVersion": 2, "specificationAssessedFields": list(FIELD_NAMES),
                  "performanceParameters": {}, "sourceManifest": manifest, "specificationEvidence": [
                      {"field": "Scale", "value": "1:100", "source_id": "mail:attachment:0", "snippet": "Scale: 1:100",
                       "location": "page 1", "scope": None, "issuer": None, "role": "requested", "rationale": "Drawing scale"}]}
    raw, _, issues = resolve_email_evidence(extraction)
    assert raw["Scale"] == "1:100" and not issues
    extraction["specificationEvidence"][0]["source_id"] = skipped["sourceId"]
    assert "Scale" in resolve_email_evidence(extraction)[2]


def test_animation_does_not_hide_real_attachment_failures():
    manifest = []
    with pytest.raises(IncompleteSourceExtraction) as captured:
        extract_text_from_email("Request", [{"filename": "signature.gif", "content": image_bytes("GIF", animated=True)},
                                            {"filename": "drawing.bin", "content": b"binary"}],
                                extractor=SimpleNamespace(max_attachment_workers=2), manifest=manifest)
    assert manifest[1]["status"] == "skipped:animated_image"
    assert manifest[2]["status"] == "failed:UnsupportedSourceFormat"
    assert "signature.gif" not in str(captured.value) and "drawing.bin" in str(captured.value)


@pytest.mark.parametrize("filename,remediation", [
    ("request.bin", "readable export"),
    ("request.rar", "readable export"),
])
@pytest.mark.parametrize("supplementary", [False, True])
def test_unsupported_sources_retain_actionable_diagnostics(tmp_path, filename, remediation, supplementary):
    client = SimpleNamespace(max_attachment_workers=1)
    with pytest.raises(IncompleteSourceExtraction) as captured:
        if supplementary:
            content = b"unsupported source"
            path = tmp_path / filename
            path.write_bytes(content)
            source = DesignEmailAsset("1", filename, filename.rsplit(".", 1)[1], len(content),
                                      "2026-10-30T12:00:00Z", "unused", False)
            downloaded = DownloadedDesignEmailAsset(source, str(path), "unused", hashlib.sha256(content).hexdigest(), len(content))
            analyze_downloaded_email_assets([downloaded], client=client)
        else:
            extract_text_from_email("Contact: Alex", [{"filename": filename, "content": b"unsupported source"}],
                                    extractor=client)
    failure = captured.value
    record = next(source for source in failure.manifest if source["filename"] == filename)
    assert record["status"] == "failed:UnsupportedSourceFormat"
    assert record["text"] == ""
    assert record["error"]["code"] == "unsupported_source_format"
    assert remediation in str(failure)
    assert remediation in record["error"]["message"]


def test_supplementary_animated_image_does_not_block_analysis(tmp_path):
    sources = [("1", "request.eml", b"From: client@example.com\nSubject: Roof\n\nContact: Alex"),
               ("2", "signature.gif", image_bytes("GIF", animated=True))]
    downloaded, contexts, image_calls = [], [], []
    for asset_id, filename, content in sources:
        path = tmp_path / filename
        path.write_bytes(content)
        source = DesignEmailAsset(asset_id, filename, filename.rsplit(".", 1)[1], len(content), "2026-10-30T12:00:00Z", "unused", False)
        downloaded.append(DownloadedDesignEmailAsset(source, str(path), "unused", hashlib.sha256(content).hexdigest(), len(content)))
    result = analyze_downloaded_email_assets(downloaded, client=SimpleNamespace(
        extract_design_parameters=lambda context: contexts.append(context) or parameters(),
        query_llm=lambda *args: "Roof", process_image=lambda *args: image_calls.append(args),
    ))
    skipped = next(source for source in result.source_manifest if source["filename"] == "signature.gif")
    assert skipped["status"] == "skipped:animated_image" and not image_calls
    assert "signature.gif" not in contexts[0] and "Contact: Alex" in contexts[0]


def gemini_reader(blocked_mime_types, finish_reason="RECITATION", block_reason=None):
    """Offline provider: blocked MIME types get no text; others are transcribed."""
    calls = []

    def generate(model, contents, config):
        mime_type = contents[0].inline_data.mime_type
        calls.append(mime_type)
        if mime_type in blocked_mime_types:
            return SimpleNamespace(text=None, candidates=[SimpleNamespace(finish_reason=finish_reason)],
                                   prompt_feedback=SimpleNamespace(block_reason=block_reason))
        return SimpleNamespace(text="Scale: 1:100", candidates=[SimpleNamespace(finish_reason="STOP")])

    return LegacyGeminiClient("offline", generate_content=generate), calls


@pytest.mark.parametrize("filename,mime_type", [("plan.png", "image/png"), ("plan.pdf", "application/pdf")])
def test_recitation_block_is_recorded_for_review_after_one_request(filename, mime_type):
    client, calls = gemini_reader({mime_type})
    content = image_bytes("PNG", animated=False) if mime_type == "image/png" else pdf_bytes()
    [record] = extract_source_records(content, filename, client, source_id="asset:1")
    assert calls == [mime_type]
    assert record["status"] == PROVIDER_RECITATION_REVIEW_STATUS and record["text"] == ""
    assert record["error"]["code"] == "blocked_response" and record["error"]["finish_reason"] == "RECITATION"
    assert record["contentSha256"] == hashlib.sha256(content).hexdigest()


def test_recitation_blocked_plans_do_not_block_readable_email_evidence():
    # Render job fb2c8adb (item 3313351849): two valid PNG plans returned
    # finish_reason=RECITATION with no text on every attempt, so extraction never completed.
    client, calls = gemini_reader({"image/png"})
    plans = [{"filename": name, "content": image_bytes("PNG", animated=False)}
             for name in ("Calder Outlet Revision - 3nr.png", "Whifflet Outlet Revisions - 3nr.png")]
    manifest = []
    context = extract_text_from_email("Contact: Alex", [{"filename": "roof.pdf", "content": pdf_bytes()}, *plans],
                                      extractor=client, manifest=manifest, source_prefix="mail")
    assert sorted(calls) == ["application/pdf", "image/png", "image/png"]
    assert "Scale: 1:100" in context and "Outlet" not in context
    blocked = [source for source in manifest if source["status"] == PROVIDER_RECITATION_REVIEW_STATUS]
    assert [source["filename"] for source in blocked] == [plan["filename"] for plan in plans]
    extraction = {"specificationEvidenceVersion": 2, "specificationAssessedFields": list(FIELD_NAMES),
                  "performanceParameters": {}, "sourceManifest": manifest, "specificationEvidence": [
                      {"field": "Scale", "value": "1:100", "source_id": "mail:attachment:0", "snippet": "Scale: 1:100",
                       "location": "page 1", "scope": None, "issuer": None, "role": "requested", "rationale": "Drawing scale"}]}
    raw, _, issues = resolve_email_evidence(extraction)
    assert raw["Scale"] == "1:100" and not issues
    # An unread plan has no text, so it can never verify a citation.
    extraction["specificationEvidence"][0]["source_id"] = blocked[0]["sourceId"]
    assert "Scale" in resolve_email_evidence(extraction)[2]


@pytest.mark.parametrize("finish_reason,block_reason", [
    ("SAFETY", None), ("MAX_TOKENS", None), ("STOP", None), ("RECITATION", "OTHER"),
])
def test_other_provider_blocks_still_hold_extraction_for_retry(finish_reason, block_reason):
    client, _ = gemini_reader({"image/png"}, finish_reason, block_reason)
    manifest = []
    with pytest.raises(IncompleteSourceExtraction) as captured:
        extract_text_from_email("Contact: Alex", [{"filename": "plan.png", "content": image_bytes("PNG", animated=False)}],
                                extractor=client, manifest=manifest)
    assert manifest[1]["status"] == "failed:GeminiRequestFailure"
    assert "plan.png" in str(captured.value)


def test_supplementary_recitation_blocked_plan_does_not_block_analysis(tmp_path):
    sources = [("1", "request.eml", b"From: client@example.com\nSubject: Roof\n\nContact: Alex"),
               ("2", "Calder Outlet Revision - 3nr.png", image_bytes("PNG", animated=False))]
    downloaded, contexts = [], []
    for asset_id, filename, content in sources:
        path = tmp_path / filename
        path.write_bytes(content)
        source = DesignEmailAsset(asset_id, filename, filename.rsplit(".", 1)[1], len(content), "2026-10-30T12:00:00Z", "unused", False)
        downloaded.append(DownloadedDesignEmailAsset(source, str(path), "unused", hashlib.sha256(content).hexdigest(), len(content)))
    reader, calls = gemini_reader({"image/png"})
    result = analyze_downloaded_email_assets(downloaded, client=SimpleNamespace(
        extract_design_parameters=lambda context: contexts.append(context) or parameters(),
        query_llm=lambda *args: "Roof", process_image=reader.process_image,
    ))
    blocked = next(source for source in result.source_manifest if source["filename"].startswith("Calder"))
    assert blocked["status"] == PROVIDER_RECITATION_REVIEW_STATUS and calls == ["image/png"]
    assert "Calder" not in contexts[0] and "Contact: Alex" in contexts[0]


def test_parameter_recovery_revisits_missing_attachment_field_with_same_meaning():
    initial = parameters()
    initial.specification_assessed_fields = list(FIELD_NAMES)
    recovered = initial.model_copy(deep=True)
    recovered.vcl = "Polythene AVCL"
    recovered.specification_evidence = [SpecificationEvidence.model_validate({"field": "VCL", "value": "Polythene AVCL", "source_id": "sheet:1",
        "location": "Request!B7", "snippet": "B7=Polythene AVCL", "scope": None, "issuer": None,
        "role": "requested", "rationale": "Selected request form cell"})]
    # Revalidate the synthetic model response just as the SDK JSON path does.
    responses = iter([initial.model_dump(), recovered.model_dump()])
    calls = []
    def generate(model, prompt, config):
        calls.append(prompt)
        return SimpleNamespace(parsed=next(responses))
    result = LegacyGeminiClient("offline", generate_content=generate).extract_design_parameters(
        "SOURCE ID: sheet:1\nWorksheet: Request\nB7=Polythene AVCL"
    )
    assert result.vcl == "Polythene AVCL" and len(calls) == 2
    assert "Targeted evidence recovery" in calls[1] and "VCL" in calls[1]
    assert result.specification_evidence[0].location == "Request!B7"


def test_html_only_multipart_body_preserves_forwarded_boundaries():
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "current@example.com", "design@example.com", "Roof request"
    message.add_alternative("<p>Current request: target 0.25</p><blockquote><p>From: older@example.com</p><p>Older target: 0.30</p></blockquote>", subtype="html")
    _, body, attachments, _ = process_email_content(message.as_bytes(), "request.eml")
    assert "Current request: target 0.25" in body
    assert "\nFrom: older@example.com\n" in body
    assert not attachments and "<p>" not in body


def test_spreadsheet_attachment_reads_selected_cells_excludes_hidden_options_and_does_not_execute_vba(tmp_path):
    book = openpyxl.Workbook()
    book.active.title = "Request"
    book.active.append(["Membrane", "Single Ply"])
    lookup = book.create_sheet("Dropdown catalogue")
    lookup.append(["Mastic Asphalt", "Foil Faced PIR"])
    lookup.sheet_state = "hidden"
    output = io.BytesIO()
    book.save(output)
    # XLSM is read as workbook data; no VBA runtime or Excel process is involved.
    text = extract_document(output.getvalue(), "Request.xlsm", None)
    assert "Worksheet: Request" in text and "A1=Membrane" in text and "B1=Single Ply" in text
    assert "Mastic Asphalt" not in text and "Dropdown catalogue" not in text
    path = tmp_path / "Request.xlsm"
    path.write_bytes(output.getvalue())
    assert any("B1=Single Ply" in doc["text"] for doc in extract_spreadsheet_documents(str(path), path.name))


def test_uncached_formula_and_unsupported_sources_require_review():
    book = openpyxl.Workbook()
    book.active.append(["U-value", "=1/4"])
    output = io.BytesIO()
    book.save(output)
    with pytest.raises(ValueError, match="incomplete"):
        extract_document(output.getvalue(), "request.xlsx", None)
    with pytest.raises(ValueError, match="Unsupported"):
        extract_document(b"binary", "drawing.bin", None)


@pytest.mark.parametrize("in_archive", [False, True])
def test_long_workbook_cells_remain_readable_evidence(in_archive):
    book = openpyxl.Workbook()
    book.active.title = "Specification"
    book.active.append(["Scope", "Long description " * 100 + "Target U-value: 0.25"])
    output = io.BytesIO()
    book.save(output)
    book.close()
    content, filename = output.getvalue(), "schedule.xlsx"
    if in_archive:
        output = io.BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr(filename, content)
        content, filename = output.getvalue(), "request.zip"
    manifest = []
    context = extract_text_from_email("Request", [{"filename": filename, "content": content}],
                                      extractor=SimpleNamespace(), manifest=manifest)
    assert "Target U-value: 0.25" in context
    assert "[truncated]" not in context and "Extraction notice" not in context
    record = next(source for source in manifest if source["filename"].endswith("schedule.xlsx"))
    assert record["status"] == "read" and "Worksheet: Specification" in record["text"]


def test_workbook_failure_keeps_safe_reason_in_manifest_and_worker_error():
    book = openpyxl.Workbook()
    book.active.append(["PRIVATE CONTENT", "=1/4"])
    output = io.BytesIO()
    book.save(output)
    book.close()
    with pytest.raises(IncompleteSourceExtraction, match="formula results are unavailable") as captured:
        extract_text_from_email("Request", [{"filename": "request.xlsx", "content": output.getvalue()}],
                                extractor=SimpleNamespace())
    record = captured.value.manifest[1]
    assert record["error"]["code"] == "workbook_incomplete" and record["text"] == ""
    assert "PRIVATE CONTENT" not in str(captured.value) and "=1/4" not in str(captured.value)


def test_docx_attachment_retains_table_text_and_does_not_expand_xml_entities():
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Membrane: Single Ply</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
    assert extract_document(output.getvalue(), "request.docx", None) == "Membrane: Single Ply"
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE root [<!ENTITY hidden "unexpected">]><root>&hidden;</root>')
    with pytest.raises(ValueError):
        extract_document(output.getvalue(), "request.docx", None)


@pytest.mark.parametrize("error_type", [TimeoutError, ValueError])
def test_failed_attachment_records_each_source_and_never_passes_error_as_evidence(error_type):
    def fail(*args):
        raise error_type("private URL must not appear in model context")
    client = SimpleNamespace(max_attachment_workers=2, process_pdf=fail)
    manifest = []
    with pytest.raises(IncompleteSourceExtraction) as captured:
        extract_text_from_email("Request text", [{"filename": "roof.pdf", "content": pdf_bytes()}],
                                extractor=client, manifest=manifest, source_prefix="asset:1")
    assert [e["status"] for e in manifest] == ["read", "failed:" + error_type.__name__]
    assert manifest[1]["sourceId"] == "asset:1:attachment:0"
    assert manifest[1]["text"] == "" and "private URL" not in str(captured.value)


def test_multi_email_recency_uses_sent_dates_not_numeric_ids_and_never_first_old_header(tmp_path):
    downloaded = []
    for asset_id, date, body in [("99", "Mon, 26 Oct 2026 09:00:00 +0000", "Older request"),
                                 ("1", "Fri, 30 Oct 2026 15:45:00 +0000", "Latest request")]:
        content = f"From: client@example.com\nTo: design@example.com\nDate: {date}\nSubject: Roof\n\n{body}".encode()
        path = tmp_path / f"{asset_id}.eml"
        path.write_bytes(content)
        source = DesignEmailAsset(asset_id, path.name, "eml", len(content), "2026-10-30T16:00:00Z", "unused", False)
        downloaded.append(DownloadedDesignEmailAsset(source, str(path), "message/rfc822", hashlib.sha256(content).hexdigest(), len(content)))
    contexts = []
    def extract(context):
        contexts.append(context)
        result = parameters()
        result.date_received, result.hour_received = "01 Jan 2000", "00:00"
        result.specification_assessed_fields = list(FIELD_NAMES)
        return result
    result = analyze_downloaded_email_assets(downloaded, client=SimpleNamespace(
        extract_design_parameters=extract, query_llm=lambda *args: "Project",
    ))
    assert [entry.asset_id for entry in result.email_content_audit] == ["99", "1"]
    assert [entry.content_sha256 for entry in result.email_content_audit] == [item.content_sha256 for item in downloaded]
    assert contexts[0].index("Older request") < contexts[0].index("Latest request")
    assert result.parameters["Date Received"] == "30 Oct 2026"
    assert result.parameters["Hour Received"] == "15:45"
    assert result.performance_parameters["Date Received"] == "30 Oct 2026"
    assert result.specification_evidence[0]["source_id"] == "asset:1:body"


def test_achieved_values_and_unselected_options_are_not_requested_targets():
    extraction = {"specificationEvidenceVersion": 2, "specificationAssessedFields": list(FIELD_NAMES),
                  "performanceParameters": {}, "sourceManifest": [{"sourceId": "pdf:1", "sourceType": "attachment",
                    "filename": "drawing.pdf", "contentSha256": "abc", "status": "read", "text": "Achieved U-value: 0.19\nOptions: Concrete"}],
                  "specificationEvidence": []}
    for label, value, role, snippet in [("Target U Value", "0.19", "achieved", "Achieved U-value: 0.19"),
                                       ("Decking", "Concrete", "lookup", "Options: Concrete")]:
        extraction["specificationEvidence"].append({"field": label, "value": value, "role": role, "snippet": snippet,
            "source_id": "pdf:1", "location": "page 1", "scope": None, "issuer": None, "rationale": "Not a selected requested value"})
    raw, _, issues = resolve_email_evidence(extraction)
    assert raw["Target U Value"] is None and raw["Decking"] is None and not issues


def test_forwarded_message_timestamp_resolves_latest_field_and_retains_direct_requester():
    text = "Old sent: 15 October 2026 10:00\nTarget: 0.30\nLatest sent: 30 October 2026 12:00\nTarget: 0.25\nContact: Alex\nEarlier contact: Oliver"
    evidence = []
    for label, value, message_date, request_role in [
        ("Target U Value", "0.30", "15 October 2026 10:00", "forwarded_request"),
        ("Target U Value", "0.25", "30 October 2026 12:00", "direct_request"),
        ("Contact", "Oliver", "15 October 2026 10:00", "forwarded_request"),
        ("Contact", "Alex", "30 October 2026 12:00", "direct_request"),
    ]:
        evidence.append({"field": label, "value": value, "source_id": "mail", "location": "forwarded chain",
                         "snippet": text, "scope": None, "issuer": None, "role": "requested", "rationale": "Dated message",
                         "message_date": message_date, "request_role": request_role})
    extraction = {"specificationEvidenceVersion": 2, "specificationAssessedFields": list(FIELD_NAMES),
                  "performanceParameters": {}, "sourceManifest": [{"sourceId": "mail", "sourceType": "email_body",
                    "filename": "request.eml", "contentSha256": "abc", "status": "read", "text": text,
                    "observedAt": "2026-10-30T12:00:00+00:00"}], "specificationEvidence": evidence}
    raw, _, issues = resolve_email_evidence(extraction)
    assert raw["Target U Value"] == "0.25" and raw["Contact"] == "Alex" and not issues


def test_bauder_reference_cannot_become_tp_reference_from_unverified_issuer_claim():
    text = "Bauder Ref: 18930_26.01"
    extraction = {"specificationEvidenceVersion": 2, "specificationAssessedFields": list(FIELD_NAMES),
                  "performanceParameters": {"Drawing Reference": "18930_26.01"},
                  "sourceManifest": [{"sourceId": "pdf", "sourceType": "attachment", "filename": "bauder.pdf",
                                      "contentSha256": "abc", "status": "read", "text": text}],
                  "specificationEvidence": [{"field": "Drawing Reference", "value": "18930_26.01", "source_id": "pdf",
                    "location": "title block", "snippet": text, "scope": None, "issuer": "TaperedPlus",
                    "role": "requested", "rationale": "Unverified claim"}]}
    raw, _, issues = resolve_email_evidence(extraction)
    assert raw["Drawing Reference"] is None and "Drawing Reference" in issues


@pytest.mark.parametrize("general_date,expected", [("15 October 2026 10:00", "Concrete"), ("30 October 2026 12:00", "Timber (18mm)")])
def test_selected_roof_overrides_older_common_value_but_respects_later_project_change(general_date, expected):
    text = f"General sent: {general_date}\nAll roofs: Timber (18mm)\nRoof 5 sent: 20 October 2026 12:00\nRoof 5: Concrete"
    evidence = []
    for scope, value, message_date in [(None, "Timber (18mm)", general_date), ("Roof 5", "Concrete", "20 October 2026 12:00")]:
        evidence.append({"field": "Decking", "value": value, "source_id": "mail", "location": "dated requests",
                         "snippet": text, "scope": scope, "issuer": None, "role": "requested", "rationale": "Explicit scope",
                         "message_date": message_date, "request_role": "direct_request"})
    extraction = {"specificationEvidenceVersion": 2, "specificationAssessedFields": list(FIELD_NAMES),
                  "performanceParameters": {}, "sourceManifest": [{"sourceId": "mail", "sourceType": "email_body",
                    "filename": "request.eml", "contentSha256": "abc", "status": "read", "text": text,
                    "observedAt": "2026-10-30T12:00:00+00:00"}], "specificationEvidence": evidence}
    raw, _, issues = resolve_email_evidence(extraction, ["5"])
    assert raw["Decking"] == expected and not issues


def test_supplementary_monday_sources_participate_in_revision_and_exclude_generated_outputs(monkeypatch):
    import json
    monkeypatch.setattr(settings, "design_processing_source_file_column_ids", ["source_files"])
    def asset(identity, name):
        return {"id": identity, "name": name, "file_extension": name.rsplit(".", 1)[-1], "file_size": 5,
                "created_at": "2026-10-30T12:00:00Z", "public_url": "https://example.com/source", "url": None}
    item = {"id": "1", "name": "Roof", "state": "active", "board": {"id": "1882196103"}, "group": {"id": "topics"},
            "assets": [asset("1", "request.eml"), asset("2", "form.xlsm"), asset("3", "Project_Performance_Specification_Details_1_hash.pdf")],
            "column_values": [{"id": "file_mkpbm883", "value": json.dumps({"files": [{"assetId": "1"}]})},
                              {"id": "source_files", "value": json.dumps({"files": [{"assetId": "2"}, {"assetId": "3"}]})}]}
    first = parse_design_processing_target(item)
    assert [source.filename for source in first.supplementary_assets] == ["form.xlsm"]
    item["assets"][1]["file_size"] = 6
    assert first.input_revision != parse_design_processing_target(item).input_revision
    item["column_values"].pop()
    with pytest.raises(DesignProcessingInputError, match="source_files"):
        parse_design_processing_target(item)


def test_evidence_migration_adds_nullable_columns_for_existing_rows():
    from importlib import import_module
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect
    old = import_module("backend.migrations.versions.0017_performance_specification")
    new = import_module("backend.migrations.versions.0018_specification_evidence")
    engine = create_engine("sqlite://")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        old.upgrade()
        new.upgrade()
        assert "review_json" in {c["name"] for c in inspect(connection).get_columns("performance_specifications")}
        assert "source_json" in {c["name"] for c in inspect(connection).get_columns("performance_specification_artifacts")}
        new.downgrade()
        old.downgrade()
    engine.dispose()
