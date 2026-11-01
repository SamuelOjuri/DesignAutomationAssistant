"""Sanitized regressions for logo evidence, client identity and existing decks."""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from backend.app.services.design_processing_inputs import DesignEmailAsset, DownloadedDesignEmailAsset
from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.email_extraction import extract_text_from_email
from backend.app.services.legacy_enquiry.source_documents import extract_source_records
from backend.app.services.performance_specification_evidence import VALUE_CONFLICT, resolve_email_evidence
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.performance_specification_contract import (
    CADTranslation, translate_specification, validate_translation,
)
from backend.app.config import Settings, settings
from backend.app.services.specification_image_text import literal_image_regions
from backend.app.services.specification_recovery import extraction_payload, repair_image_company_citations
from backend.app.services.specification_request_context import email_messages, select_client_request
from backend.tests.test_specification_evidence import candidate, extraction, source
from backend.tests.test_specification_publication_hardening import extract_twice, parameters
from backend.tests.test_performance_specification import case, current, pdf_text, run, translation_payload
from backend.tests.test_specification_sources import image_bytes


CLIENT_DATE = "Mon, 26 Oct 2026 14:56:06 +0000"
INTERNAL_DATE = "Mon, 26 Oct 2026 16:49:48 +0000"
BRAND = "example roofing & construction"
REGION = f"[TEXT REGION 1]\n{BRAND}\n[/TEXT REGION 1]\nVISUAL CONTEXT\nA logo with a central emblem."
CLIENT = (f"From: Alex Example <alex@client.example>\nTo: Sales <sales@taperedplus.co.uk>\n"
          f"Subject: Roof\nDate: {CLIENT_DATE}\n\nCompany: {BRAND}\nContact: Alex Example\n"
          "[cid:logo@client]\n")


def image_source(identity="asset:2:attachment:0", text=REGION):
    record = source(identity, text)
    record.update(filename="logo.png", imageTextRegions=literal_image_regions(text))
    return record


def test_split_logo_citation_is_repaired_only_from_its_literal_image_region():
    manifest = [image_source()]
    old = candidate("Company", BRAND, "Left text example; right text roofing & construction",
                    source_id=manifest[0]["sourceId"], request_role="direct_request")
    result = parameters(company=BRAND, specification_evidence=[old])
    repair_image_company_citations(result, manifest)
    raw, _, issues = resolve_email_evidence(extraction_payload(result, manifest))
    assert not issues and raw["Company"] == BRAND
    repair, = result._evidence_repairs
    assert repair["before"]["snippet"] == old["snippet"]
    assert repair["after"]["snippet"] == BRAND and repair["regionId"] == "1"
    assert repair["contentSha256"] == "abc"


def test_descriptive_prose_cannot_establish_an_image_company_or_constraint():
    record = image_source(text=REGION + "\nCompany: Example Roofing Ltd\nMaximum height 170 mm")
    company = candidate("Company", "Example Roofing Ltd", "Company: Example Roofing Ltd",
                        source_id=record["sourceId"], request_role="direct_request")
    assert "Company" in resolve_email_evidence(extraction([record], [company]))[2]
    from backend.app.services.performance_specification_evidence import request_note_diagnostics
    data = extraction([record], [])
    data["specificationRequestNotes"] = [{"source_id": record["sourceId"], "snippet": "Maximum height 170 mm",
                                         "scope": None, "category": "constraint"}]
    assert request_note_diagnostics(data)


@pytest.mark.parametrize("literal", [True, False])
def test_image_reader_records_literal_regions_and_rejects_description_only_values(literal):
    transcript = REGION if literal else "VISUAL CONTEXT\nA company called " + BRAND
    client = LegacyGeminiClient("offline", generate_content=lambda *args: SimpleNamespace(text=transcript))
    record, = extract_source_records(image_bytes("PNG", animated=False), "logo.png", client,
                                    source_id="asset:1:attachment:0")
    company = candidate("Company", BRAND, BRAND, request_role="direct_request")
    raw, _, issues = resolve_email_evidence(extraction([record], [company]))
    assert bool(record["imageTextRegions"]) == literal
    assert (raw["Company"] == BRAND and not issues) if literal else "Company" in issues


@pytest.mark.parametrize("case", ["changed_name", "separate_regions", "duplicate_region", "invented_metadata"])
def test_logo_repairs_cannot_invent_names_join_regions_or_trust_metadata_alone(case):
    text, value = REGION, BRAND
    if case == "changed_name":
        value += " Ltd"
    elif case == "separate_regions":
        text = "[TEXT REGION 1]\nexample\n[/TEXT REGION 1]\n[TEXT REGION 2]\nroofing & construction\n[/TEXT REGION 2]"
    elif case == "duplicate_region":
        text = REGION + "\n" + REGION
    elif case == "invented_metadata":
        text = "An unreadable logo."
    record = image_source(text=text)
    record["imageTextRegions"] = [{"id": "1", "text": value}]
    result = parameters(company=value, specification_evidence=[
        candidate("Company", value, "Noncontiguous invented excerpt",
                  source_id=record["sourceId"], request_role="direct_request")])
    repair_image_company_citations(result, [record])
    assert not result._evidence_repairs
    assert "Company" in resolve_email_evidence(extraction_payload(result, [record]))[2]


def test_logo_recovery_cannot_omit_the_original_unverified_claim():
    old = candidate("Company", BRAND, "Stitched logo description", request_role="direct_request")
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(parameters(company=BRAND, specification_evidence=[old]), parameters(),
                      [source(old["source_id"], "A descriptive logo without a literal name")])


def test_client_identity_excludes_later_internal_followups():
    internal = (f"From: Engineer <engineer@taperedplus.co.uk>\nTo: Sales <sales@taperedplus.co.uk>\n"
                f"Date: {INTERNAL_DATE}\n\nCompany: TaperedPlus\nContact: Internal Engineer\n")
    manifest = [source("asset:1:body", internal, "email_body"), source("asset:2:body", CLIENT, "email_body")]
    evidence = [
        candidate("Company", "TaperedPlus", "Company: TaperedPlus", source_id="asset:1:body", request_role="direct_request"),
        candidate("Contact", "Internal Engineer", "Contact: Internal Engineer", source_id="asset:1:body", request_role="direct_request"),
        candidate("Company", BRAND, "Company: " + BRAND, source_id="asset:2:body", request_role="direct_request"),
        candidate("Contact", "Alex Example", "Contact: Alex Example", source_id="asset:2:body", request_role="direct_request"),
    ]
    context = select_client_request(manifest)
    assert context["sourceId"] == "asset:2:body" and context["sentDate"] == CLIENT_DATE
    raw, _, issues = resolve_email_evidence(extraction(manifest, evidence))
    assert not issues and raw["Company"] == BRAND and raw["Contact"] == "Alex Example"


def test_indented_body_is_not_a_folded_date_header():
    text = CLIENT.replace("Company: " + BRAND, "    Maximum height 130 mm\nCompany: " + BRAND)
    message, = email_messages(text)
    assert message["sentDate"] == CLIENT_DATE
    assert "Maximum height" not in message["headerSnippet"]


def test_repeated_client_signature_can_identify_company_without_dating_unrelated_decking():
    older = CLIENT.replace("Date: " + CLIENT_DATE, "Sent: 20 October 2026 13:33")
    parent = source("asset:2:body", CLIENT + "\n" + older, "email_body")
    logo = image_source()
    logo["contentId"] = "logo@client"
    evidence = candidate("Company", BRAND, BRAND, source_id=logo["sourceId"],
                         request_role="direct_request", message_date=CLIENT_DATE)
    raw, _, issues = resolve_email_evidence(extraction([parent, logo], [evidence]))
    assert not issues and raw["Company"] == BRAND


def test_client_evidence_cannot_borrow_another_messages_literal_header_date():
    earlier = CLIENT.replace("Date: " + CLIENT_DATE, "Sent: 20 October 2026 13:33").replace(
        "Company: " + BRAND, "An earlier proposal")
    parent = source("asset:2:body", CLIENT + "\n" + earlier, "email_body")
    evidence = candidate("Company", BRAND, "Company: " + BRAND, source_id=parent["sourceId"],
                         request_role="direct_request", message_date="20 October 2026 13:33")
    assert resolve_email_evidence(extraction([parent], [evidence]))[2]["Company"] == "Candidate request timestamp could not be verified"


def test_forwarded_client_request_is_usable_without_its_original_outer_email():
    forwarded = CLIENT.replace("Date: " + CLIENT_DATE, "Sent: 26 October 2026 14:56")
    forwarded = forwarded.replace("alex@client.example>", "alex@client.example<mailto:alex@client.example>>")
    text = (f"From: Engineer <engineer@taperedplus.co.uk>\nTo: Sales <sales@taperedplus.co.uk>\n"
            f"Date: {INTERNAL_DATE}\n\nPlease action this request.\n" + forwarded)
    manifest = [source("asset:1:body", text, "email_body")]
    context = select_client_request(manifest)
    assert context["sender"] == "alex@client.example" and context["messageIndex"] == 1
    evidence = candidate("Company", BRAND, "Company: " + BRAND, source_id="asset:1:body",
                         request_role="forwarded_request", message_date=context["sentDate"])
    assert resolve_email_evidence(extraction(manifest, [evidence]))[0]["Company"] == BRAND


def test_signature_from_an_earlier_forwarded_sender_is_not_the_requester():
    earlier = CLIENT.replace("Alex Example", "Other Sender").replace("alex@client.example", "other@third.example")
    text = CLIENT + "\n" + earlier
    record = source("asset:2:body", text, "email_body")
    evidence = candidate("Contact", "Other Sender", "From: Other Sender <other@third.example>",
                         source_id=record["sourceId"], request_role="direct_request")
    # Equal timestamps from different clients must be resolved, never chosen by ID.
    with pytest.raises(ValueError, match="conflicting senders"):
        resolve_email_evidence(extraction([record], [evidence]))


def forwarded_image_sources(*, duplicate=False):
    text = (f"From: Engineer <engineer@taperedplus.co.uk>\nTo: Sales <sales@taperedplus.co.uk>\n"
            f"Date: {INTERNAL_DATE}\n\nReview.\n"
            "From: Architect <architect@drawing.example>\nTo: Engineer <engineer@taperedplus.co.uk>\n"
            "Sent: 22 October 2026 16:36\nSubject: Roof detail\n\n[cid:plan@drawing]\n")
    if duplicate:
        text += ("From: Architect <architect@drawing.example>\nSent: 21 October 2026 16:36\n"
                 "Subject: Earlier detail\n\n[cid:plan@drawing]\n")
    parent = source("asset:1:body", text, "email_body")
    plan = source("asset:1:attachment:0", "Paving on adjustable feet - laid over polyroof deck")
    plan["contentId"] = "plan@drawing"
    return [parent, plan]


def test_forwarded_image_date_is_verified_by_its_unique_content_id_message():
    manifest = forwarded_image_sources()
    evidence = candidate("Decking", "polyroof deck", manifest[1]["text"],
                         role="baseline", request_role="forwarded_request", message_date="22 October 2026 16:36")
    raw, provenance, issues = resolve_email_evidence(extraction(manifest, [evidence]))
    assert not issues and raw["Decking"] == "polyroof deck"
    selected = provenance["Decking"]["selected"]
    assert selected["observedAt"] == "2026-10-22T15:36:00+00:00"
    assert selected["sourceObservedAt"] != selected["observedAt"]


@pytest.mark.parametrize("case", ["reused_cid", "unidentified_reused_cid", "wrong_cid", "wrong_date", "wrong_message_role", "body_date", "different_parent_date"])
def test_unverified_image_message_association_still_blocks(case):
    manifest = forwarded_image_sources(duplicate=case in {"reused_cid", "unidentified_reused_cid"})
    evidence = candidate("Decking", "polyroof deck", manifest[1]["text"], role="baseline",
                         request_role="forwarded_request", message_date="22 October 2026 16:36")
    if case == "wrong_cid":
        manifest[1]["contentId"] = "plan@drawing-other"
    elif case == "wrong_date":
        evidence["message_date"] = INTERNAL_DATE
    elif case == "wrong_message_role":
        evidence["request_role"] = "direct_request"
    elif case == "body_date":
        manifest[0]["text"] = manifest[0]["text"].replace("Sent: 22 October 2026 16:36",
            "Subject: Roof\n\nUnverified body date: 22 October 2026 16:36")
    elif case == "different_parent_date":
        manifest[1]["observedAt"] = "2026-10-01T09:00:00Z"
    elif case == "unidentified_reused_cid":
        manifest[0]["text"] = manifest[0]["text"].replace(
            "From: Architect <architect@drawing.example>\nSent:", "From: Architect\nSent:")
    _, _, issues = resolve_email_evidence(extraction(manifest, [evidence]))
    assert issues["Decking"] == "Candidate request timestamp could not be verified"


def test_unassociated_document_is_not_dated_by_the_email_that_carries_it():
    first = source("asset:1:attachment:0", "Deck: Concrete")
    second = source("asset:2:attachment:0", "Deck: Timber (18mm)")
    first["observedAt"], second["observedAt"] = "2026-10-01T09:00:00Z", "2026-10-30T09:00:00Z"
    data = extraction([first, second], [
        candidate("Decking", "Concrete", first["text"], source_id=first["sourceId"], role="baseline"),
        candidate("Decking", "Timber (18mm)", second["text"], source_id=second["sourceId"], role="baseline"),
    ])
    raw, provenance, issues = resolve_email_evidence(data)
    assert raw["Decking"] is None and issues["Decking"] == VALUE_CONFLICT
    assert all(item["observedAt"] is None for item in provenance["Decking"]["candidates"])


def test_existing_deck_is_retained_but_achieved_targets_and_unselected_options_are_not():
    text = "Existing deck: Concrete\nAchieved U-value: 0.38\nUnselected VCL: Polythene AVCL"
    data = extraction([source("asset:1:attachment:0", text)], [
        candidate("Decking", "Concrete", "Existing deck: Concrete", role="baseline"),
        candidate("Target U Value", "0.38", "Achieved U-value: 0.38", role="achieved"),
        candidate("VCL", "Polythene AVCL", "Unselected VCL: Polythene AVCL", role="lookup"),
    ])
    raw, _, issues = resolve_email_evidence(data)
    assert not issues and raw["Decking"] == "Concrete"
    assert raw["Target U Value"] is None and raw["VCL"] is None


def test_recovery_cannot_clear_applicable_baseline_decking():
    text = "Existing deck: polyroof deck"
    evidence = candidate("Decking", "polyroof deck", text, role="baseline")
    result = extract_twice(parameters(decking="polyroof deck", specification_evidence=[evidence]),
                          parameters(decking=None, specification_evidence=[evidence]),
                          [source(evidence["source_id"], text)])
    assert result.decking == "polyroof deck"
    assert resolve_email_evidence(extraction_payload(result, [source(evidence["source_id"], text)]))[0]["Decking"] == "polyroof deck"
    assert not any(repair.get("field") == "Decking" for repair in result._evidence_repairs)


def test_reformatted_forwarded_image_date_can_be_repaired_without_moving_the_fact():
    manifest = forwarded_image_sources()
    old = candidate("Decking", "polyroof deck", manifest[1]["text"], role="baseline",
                    request_role="forwarded_request", message_date="2026-10-22T16:36:00Z")
    corrected = {**old, "message_date": "22 October 2026 16:36"}
    result = extract_twice(parameters(decking="polyroof deck", specification_evidence=[old]),
                          parameters(decking="polyroof deck", specification_evidence=[corrected]), manifest)
    assert result._evidence_repairs[0]["before"]["message_date"] == old["message_date"]
    assert not resolve_email_evidence(extraction_payload(result, manifest))[2]


def test_inline_extraction_retains_verifiable_containing_message_metadata():
    text = forwarded_image_sources()[0]["text"]
    # No actual image decoding is needed to test the MIME-to-message association.
    from unittest.mock import patch
    with patch("backend.app.services.legacy_enquiry.email_extraction.extract_source_records",
               return_value=[source("asset:1:attachment:0", "Deck: Concrete")]):
        manifest = []
        extract_text_from_email(text, [], extractor=SimpleNamespace(max_attachment_workers=1),
            inline_images=[{"filename": "plan.png", "content": b"synthetic", "content_id": "<plan@drawing>"}],
            manifest=manifest, source_prefix="asset:1", observed_at="2026-10-30T11:32:17+00:00")
    assert manifest[1]["containingMessage"]["sentDate"] == "22 October 2026 16:36"
    assert "text" not in manifest[1]["containingMessage"]


def downloaded_email(tmp_path, identity, text):
    path = tmp_path / f"{identity}.eml"
    content = text.encode()
    path.write_bytes(content)
    asset = DesignEmailAsset(identity, path.name, ".eml", len(content), "", "", False)
    return DownloadedDesignEmailAsset(asset, str(path), "message/rfc822", hashlib.sha256(content).hexdigest(), len(content))


@pytest.mark.parametrize("forwarded_only", [False, True])
def test_analysis_uses_client_context_for_canonical_fields_and_date(tmp_path, forwarded_only):
    internal = (f"From: Engineer <engineer@taperedplus.co.uk>\nTo: Sales <sales@taperedplus.co.uk>\n"
                f"Subject: Roof\nDate: {INTERNAL_DATE}\n\nReview request.\n")
    if forwarded_only:
        internal += CLIENT.replace("Date: " + CLIENT_DATE, "Sent: 26 October 2026 14:56")
    assets = [downloaded_email(tmp_path, "1", internal)]
    if not forwarded_only:
        assets.append(downloaded_email(tmp_path, "2", CLIENT))
    identity = "asset:1:body" if forwarded_only else "asset:2:body"
    role = "forwarded_request" if forwarded_only else "direct_request"
    def extract(context, manifest):
        return parameters(company=BRAND, contact="Alex Example", specification_evidence=[
            candidate("Company", BRAND, "Company: " + BRAND, source_id=identity, request_role=role),
            candidate("Contact", "Alex Example", "Contact: Alex Example", source_id=identity, request_role=role)])
    result = analyze_downloaded_email_assets(assets, client=SimpleNamespace(
        extract_design_parameters_with_manifest=extract, query_llm=lambda *args: "Example Roof"))
    assert result.performance_parameters["Company"] == BRAND and result.performance_parameters["Contact"] == "Alex Example"
    assert result.performance_parameters["Date Received"] == "26 Oct 2026"
    assert result.parameters["Hour Received"] == "14:56"
    assert next(item for item in result.specification_evidence if item["field"] == "Date Received")["request_role"] == role


def test_internal_only_correspondence_does_not_invent_a_client_date(tmp_path):
    text = (f"From: Engineer <engineer@taperedplus.co.uk>\nTo: Sales <sales@taperedplus.co.uk>\n"
            f"Subject: Roof\nDate: {INTERNAL_DATE}\n\nSchedule a visit.")
    result = analyze_downloaded_email_assets([downloaded_email(tmp_path, "1", text)],
        client=SimpleNamespace(extract_design_parameters=lambda *args: parameters(date_received="26 Oct 2026"),
                               query_llm=lambda *args: "Example Roof"))
    assert result.performance_parameters["Date Received"] is None
    assert result.parameters["Date Received"] == result.parameters["Hour Received"] == "Not provided"


def test_ambiguous_existing_deck_publishes_literal_review_instead_of_missing_or_guessed_material(case):
    data = deepcopy(case.item.extracted_parameters_json)
    data["performanceParameters"]["Decking"] = "polyroof deck"
    data["specificationEvidence"] = [e for e in data["specificationEvidence"] if e["field"] != "Decking"]
    data["sourceManifest"].append(source("asset:1:attachment:0", "Existing deck: polyroof deck"))
    data["specificationEvidence"].append(candidate("Decking", "polyroof deck", "Existing deck: polyroof deck", role="baseline"))
    case.item.extracted_parameters_json = data
    case.db.commit()
    def generate(model, prompt, config):
        raw = json.loads(prompt.split("\nResolved values: ", 1)[1])
        assert raw["Decking"] == "polyroof deck"
        payload = translation_payload({**raw, "Decking": None})
        payload["warnings"] = ["Decking: polyroof deck does not identify a structural material."]
        return SimpleNamespace(parsed=payload)
    case.client = LegacyGeminiClient("offline", generate_content=generate)
    assert run(case) == ["published"]
    record = current(case)
    assert record.review_json["status"] == "published_with_review"
    assert record.source_json["sources"]["Decking"]["selected"]["role"] == "baseline"
    text = pdf_text(case.gateway.uploads[-1][1])
    assert "Decking: Review required" in text and "polyroof deck" in text
    assert "Decking: Not Provided" not in text


def test_client_domain_configuration_changes_the_extraction_identity(monkeypatch):
    first = settings.design_processing_pipeline_version
    monkeypatch.setattr(settings, "design_processing_internal_email_domains", "other.example")
    assert settings.design_processing_pipeline_version != first


@pytest.mark.parametrize("value", ["", "@taperedplus.co.uk", "https://taperedplus.co.uk"])
def test_invalid_internal_domain_configuration_is_rejected(value):
    required = {name: getattr(settings, name) for name, field in Settings.model_fields.items() if field.is_required()}
    with pytest.raises(ValueError, match="Internal email domains"):
        Settings(_env_file=None, **required, design_processing_internal_email_domains=value)


def test_internal_domains_are_normalized_before_forming_the_identity():
    required = {name: getattr(settings, name) for name, field in Settings.model_fields.items() if field.is_required()}
    first = Settings(_env_file=None, **required, design_processing_internal_email_domains=" TAPEREDPLUS.co.uk,other.example,taperedplus.co.uk ")
    second = Settings(_env_file=None, **required, design_processing_internal_email_domains="other.example,taperedplus.co.uk")
    assert first.design_processing_pipeline_version == second.design_processing_pipeline_version


@pytest.mark.parametrize("field,raw,formatted", [
    ("Company", "example roofing &\nconstruction", "example roofing & construction"),
    ("Contact", "Alex\n  Example", "Alex Example"),
    ("Drawing Title", "Proposed Drive Thru Restaurant\nTrentham Gardens",
     "Proposed Drive Thru Restaurant Trentham Gardens"),
])
def test_cad_text_can_collapse_source_layout_whitespace(field, raw, formatted):
    payload = translation_payload({field: formatted})
    translation = CADTranslation.model_validate(payload)
    assert validate_translation(translation, {field: raw}) is translation


def test_cad_text_still_rejects_line_breaks_in_the_translated_value():
    title = "Proposed Drive Thru Restaurant\nTrentham Gardens"
    translation = CADTranslation.model_validate(translation_payload({"Drawing Title": title}))
    with pytest.raises(ValueError, match="control characters in Drawing Title"):
        validate_translation(translation, {"Drawing Title": title})


def test_cad_translation_receives_single_line_title_block_text():
    prompts = []

    def generate(model, prompt, config):
        prompts.append(prompt)
        # The model copies the resolved value exactly, as instructed.
        return SimpleNamespace(parsed=translation_payload(json.loads(prompt.split("\nResolved values: ", 1)[1])))

    client = LegacyGeminiClient("gemini-3.8-flash", generate_content=generate)
    translation = translate_specification(
        {"Drawing Title": "Proposed Drive Thru Restaurant\nTrentham Gardens"}, client,
    )
    assert translation.values.model_dump(by_alias=True)["Drawing Title"] == (
        "Proposed Drive Thru Restaurant Trentham Gardens"
    )
    assert len(prompts) == 1


@pytest.mark.parametrize("changed", ["Example Roofing & construction", "example roofing & construction Ltd",
                                   "example roofing construction", "exampleroofing & construction"])
def test_cad_name_whitespace_normalization_cannot_change_branding(changed):
    translation = CADTranslation.model_validate(translation_payload({"Company": changed}))
    with pytest.raises(ValueError, match="changed source text"):
        validate_translation(translation, {"Company": "example roofing &\nconstruction"})
