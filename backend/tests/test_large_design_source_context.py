from email.message import EmailMessage
import hashlib
from types import SimpleNamespace

import pytest

from backend.app.services.design_processing_inputs import DesignEmailAsset, DownloadedDesignEmailAsset
from backend.app.services.legacy_enquiry import source_context
from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.legacy_enquiry.source_analysis_failures import SourceAnalysisReviewRequired
from backend.app.services.legacy_enquiry.source_context import MAX_CONTEXT_BATCHES, bounded_source_contexts
from backend.app.services.legacy_enquiry.source_documents import MAX_CONTEXT_CHARS, source_record
from backend.app.services.specification_failures import SpecificationEvidenceError
from backend.app.services.specification_semantics import FIELD_DEFINITIONS
from backend.tests.test_specification_sources import image_bytes


def _email_message(body, date):
    message = EmailMessage()
    message["From"] = "requester@client.example"
    message["To"] = "sales@taperedplus.co.uk"
    message["Subject"] = "Synthetic project"
    message["Date"] = date
    message.set_content(body)
    return message


def _email(tmp_path, asset_id, body, date):
    return _downloaded_email(tmp_path, asset_id, _email_message(body, date))


def _downloaded_email(tmp_path, asset_id, message):
    content = message.as_bytes()
    path = tmp_path / f"request-{asset_id}.eml"
    path.write_bytes(content)
    asset = DesignEmailAsset(asset_id, path.name, ".eml", len(content), "2026-11-27T13:00:00Z", "", False)
    return DownloadedDesignEmailAsset(asset, str(path), "message/rfc822", hashlib.sha256(content).hexdigest(), len(content))


def _evidence(field, value, snippet, source_id, *, scope=None):
    return {"field": field, "value": value, "snippet": snippet, "source_id": source_id,
            "location": "Current request body", "scope": scope, "issuer": None, "role": "requested",
            "request_role": "direct_request", "rationale": "Explicit requested fact"}


@pytest.mark.parametrize("conflicting_scope", [False, True])
def test_large_email_bundle_resolves_verified_chronology_and_scope(tmp_path, conflicting_scope):
    title = "Drawing title: Synthetic project"
    prior_target = "Roof A Target U-value: 0.20 W/m²K"
    later_scope = "Roof B" if conflicting_scope else "Roof A"
    later_target = f"{later_scope} Target U-value: 0.15 W/m²K"
    # Each email fits but their combined source context exceeds the job limit.
    filler = "Synthetic padding text.\n" * 11_000
    assets = [
        _email(tmp_path, "1", f"{prior_target}\n{filler}", "Thu, 26 Nov 2026 10:31:00 +0000"),
        _email(tmp_path, "2", f"{title}\n{later_target}\n{filler}", "Fri, 27 Nov 2026 11:42:00 +0000"),
    ]
    batches = []

    def model_reply(model, prompt, config):
        context = prompt.split("QUESTION:", 1)[0]
        assert "one batch of a larger source inventory" in prompt
        assert len(context) <= MAX_CONTEXT_CHARS + 140
        assert ("SOURCE ID: asset:1:body" in context) != ("SOURCE ID: asset:2:body" in context)
        batches.append(context)
        if "SOURCE ID: asset:1:body" in context:
            target, scope, source_id = prior_target, "Roof A", "asset:1:body"
        else:
            target, scope, source_id = later_target, later_scope, "asset:2:body"
        value = target.split(": ", 1)[1]
        evidence = [_evidence("Target U Value", value, target, source_id, scope=scope)]
        data = {name: None for name, field in DesignParameterExtraction.model_fields.items() if field.is_required()}
        if source_id == "asset:2:body":
            data["drawing_title"] = "Synthetic project"
            evidence.append(_evidence("Drawing Title", data["drawing_title"], title, source_id))
        return SimpleNamespace(parsed=DesignParameterExtraction.model_validate({
            **data, "target_u_value": value, "specification_evidence": evidence,
            "specification_assessed_fields": sorted(set(FIELD_DEFINITIONS) - {"Incoming Information"}),
        }))

    result = analyze_downloaded_email_assets(assets, client=LegacyGeminiClient("offline", generate_content=model_reply))

    assert len(batches) == 2
    assert result.project_name == "Synthetic project"
    assert result.parameters["Hour Received"] == "11:42"
    assert result.parameters["Target U-Value"] == ("Not provided" if conflicting_scope else "0.15 W/m²K")
    assert {entry["source_id"] for entry in result.specification_evidence if entry["field"] == "Target U Value"} == {
        "asset:1:body", "asset:2:body",
    }
    assert len(result.source_manifest) == 2
    assert len(result.email_content_audit) == 2


@pytest.mark.parametrize("batch_limit", [MAX_CONTEXT_BATCHES, 1])
def test_large_single_email_tree_preserves_sources_and_obeys_batch_capacity(tmp_path, monkeypatch, batch_limit):
    monkeypatch.setattr(source_context, "MAX_CONTEXT_BATCHES", batch_limit)
    title = "Drawing title: Synthetic project"
    mail = _email_message(title, "Fri, 27 Nov 2026 11:42:00 +0000")
    mail.add_alternative('<p>See the attached notes.</p><img src="cid:detail">', subtype="html")
    mail.get_payload()[1].add_related(image_bytes("PNG", animated=False), maintype="image", subtype="png",
                                      cid="<detail>", filename="detail.png")
    nested = _email_message("Earlier correspondence", "Thu, 26 Nov 2026 11:42:00 +0000")
    nested_text = "Nested document text.\n" * 10_000
    nested.add_attachment(nested_text.encode(), maintype="text", subtype="plain", filename="nested.txt")
    mail.add_attachment(nested, filename="history.eml")
    first_text = "First document text.\n" * 10_000
    last_text = "Last document text.\n" * 10_000
    for filename, contents in (("first.txt", first_text), ("last.txt", last_text)):
        mail.add_attachment(contents.encode(), maintype="text", subtype="plain", filename=filename)
    mail.add_attachment(b"Synthetic CAD source", maintype="application", subtype="octet-stream", filename="plan.dwg")

    asset = _downloaded_email(tmp_path, "1", mail)
    batches, progress = [], []
    image_text = "Readable inline detail"

    def extract_batches(contexts, manifest):
        batches.extend(contexts)
        data = {name: None for name, field in DesignParameterExtraction.model_fields.items() if field.is_required()}
        return DesignParameterExtraction.model_validate({
            **data, "drawing_title": "Synthetic project",
            "specification_evidence": [_evidence("Drawing Title", "Synthetic project", title, "asset:1:body")],
            "specification_assessed_fields": sorted(set(FIELD_DEFINITIONS) - {"Incoming Information"}),
        })

    client = SimpleNamespace(
        max_attachment_workers=1,
        process_image=lambda *_: image_text,
        extract_design_parameters_batches=extract_batches,
        extract_design_parameters=lambda *_: pytest.fail("Expected bounded context batches"),
        query_llm=lambda *_: pytest.fail("Expected the verified title from the email body"),
    )
    if batch_limit == 1:
        with pytest.raises(SourceAnalysisReviewRequired) as error:
            analyze_downloaded_email_assets([asset], client=client, on_progress=progress.append)
        assert error.value.details["code"] == "combined_source_text_limit"
        assert error.value.details["scope"] == "item"
        assert error.value.details["batch_limit"] == 1
        assert not batches
        assert progress[-1]["source_counts"] == {"total": 7, "read": 6, "skipped": 1, "review": 0, "failed": 0}
        return

    result = analyze_downloaded_email_assets([asset], client=client, on_progress=progress.append)

    assert len(batches) == 2
    assert all(len(batch) <= MAX_CONTEXT_CHARS for batch in batches)
    assert result.project_name == "Synthetic project"
    sources = {record["sourceId"]: record for record in result.source_manifest}
    expected = {
        "asset:1:body": "read",
        "asset:1:attachment:0:body": "read",
        "asset:1:attachment:0:attachment:0": "read",
        "asset:1:attachment:1": "read",
        "asset:1:attachment:2": "read",
        "asset:1:attachment:3": "skipped:dwg",
        "asset:1:attachment:4": "read",
    }
    assert {source_id: record["status"] for source_id, record in sources.items()} == expected
    assert sources["asset:1:attachment:0:body"]["parentSourceId"] == "asset:1:body"
    assert sources["asset:1:attachment:0:attachment:0"]["parentSourceId"] == "asset:1:attachment:0:body"
    assert sources["asset:1:attachment:4"]["contentId"] == "detail"
    for source_id, contents in (
        ("asset:1:attachment:0:attachment:0", nested_text),
        ("asset:1:attachment:1", first_text),
        ("asset:1:attachment:2", last_text),
        ("asset:1:attachment:4", image_text),
    ):
        assert sources[source_id]["text"] == contents
        assert any(f"SOURCE ID: {source_id}\n" in batch and contents in batch for batch in batches)
    assert all("SOURCE ID: asset:1:attachment:3\n" not in batch for batch in batches)


def test_large_source_context_preserves_source_id_and_overlap():
    text = ("line one\n" * 200) + "boundary excerpt with a required fact" + ("\nline two" * 200)
    record = source_record("asset:1", "source.txt", text.encode(), source_type="attachment", text=text)
    contexts = bounded_source_contexts([record], limit=1_500, character_count=len(text))

    assert len(contexts) > 1
    assert all(len(context) <= 1_500 and "SOURCE ID: asset:1" in context for context in contexts)
    assert any("boundary excerpt with a required fact" in context for context in contexts)
    assert record["text"] == text


def test_large_source_batch_rejects_populated_values_from_unadopted_history():
    outer = ("From: Client <client@client.example>\nTo: Sales <sales@taperedplus.co.uk>\n"
             "Subject: Synthetic project\nDate: Fri, 27 Nov 2026 11:00:00 +0000\n\n"
             "Please start a new layout. Attached correspondence is for information only.")
    history = ("From: Previous Designer <designer@previous.example>\nTo: Client <client@client.example>\n"
               "Subject: Earlier project\nDate: Thu, 26 Nov 2026 11:00:00 +0000\n\nScale: 1:100")
    source = source_record("asset:1:body", "Current email", outer.encode(), source_type="email_body", text=outer)
    attached = source_record("asset:1:attachment:0:body", "Historical email", history.encode(),
                             source_type="email_body", text=history)
    attached.update(parentSourceId=source["sourceId"], attachedEmail=True)

    def model_reply(model, prompt, config):
        data = {name: None for name, field in DesignParameterExtraction.model_fields.items() if field.is_required()}
        if "ATTACHED QUOTATION" in prompt:
            data["scale"] = "1:100"
            data["specification_evidence"] = [_evidence("Scale", "1:100", "Scale: 1:100", attached["sourceId"])]
        data["specification_assessed_fields"] = sorted(set(FIELD_DEFINITIONS) - {"Incoming Information"})
        return SimpleNamespace(parsed=DesignParameterExtraction.model_validate(data))

    client = LegacyGeminiClient("offline", generate_content=model_reply)
    with pytest.raises(SpecificationEvidenceError, match="Populated extraction has no verified applicable evidence"):
        client.extract_design_parameters_batches((outer, "ATTACHED QUOTATION\n" + history), [source, attached])


def test_large_source_context_capacity_failure_lists_only_safe_diagnostics():
    text = "private source text " * 100
    records = [source_record(f"asset:{asset_id}", "sensitive name.txt", text.encode(),
                             source_type="attachment", text=text) for asset_id in range(MAX_CONTEXT_BATCHES + 1)]

    with pytest.raises(SourceAnalysisReviewRequired) as failure:
        bounded_source_contexts(records, limit=2_500, character_count=len(text) * len(records))

    assert failure.value.details == {
        "code": "combined_source_text_limit", "scope": "item",
        "character_count": len(text) * len(records), "character_limit": 2_500, "batch_limit": MAX_CONTEXT_BATCHES,
        "largest_sources": [{"source_id": f"asset:{asset_id}", "character_count": len(text)} for asset_id in range(5)],
    }
