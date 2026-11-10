"""Intent comes from the current client request, never project-name similarity."""

import pytest
import json
from types import SimpleNamespace

from backend.app.config import settings
from backend.app.services.legacy_enquiry.parameter_extraction import (
    CANONICAL_PARAMETER_ORDER, DesignParameterExtraction, PARAMETER_EXTRACTION_PROMPT,
)
from backend.app.services.specification_intent import (
    build_request_intent, enrich_request_intent, reason_for_change,
)
from backend.app.services.specification_review_contract import RequestIntentRecommendation


@pytest.fixture(autouse=True)
def internal_domains(monkeypatch):
    monkeypatch.setattr(settings, "design_processing_internal_email_domains", "taperedplus.co.uk")


def email(body, *, identity="asset:1:body", sender="client@example.com", date="9 Nov 2026 10:00:00 +0000"):
    return {"sourceId": identity, "sourceType": "email_body", "status": "read",
            "text": f"From: Client <{sender}>\nTo: team@taperedplus.co.uk\nSubject: School roof\nDate: {date}\n\n{body}"}


def proposal(kind, excerpt, *, source_id="asset:1:body", **extra):
    return {"recommended_type": kind, "explanation": "Current client request supports this recommendation.",
            "evidence": [{"source_id": source_id, "excerpt": excerpt, "location": "email body"}], **extra}


def match(item_id="123", similarity=0.99):
    return {"record": {"board_id": "900", "item_id": item_id},
            "rationale": "Same school title", "similarity_score": similarity}


@pytest.mark.parametrize("body,kind", [
    ("This is a new enquiry for another building at the same school.", "new_enquiry"),
    ("Please adjust the scheme to suit the revised falls.", "amendment"),
    ("Could you change the U-value to 0.12?", "amendment"),
    ("Please provide a quotation for the school roof.", "uncertain"),
    ("This is a new enquiry. Please amend the previous scheme.", "uncertain"),
    ("No amendment is requested.", "uncertain"),
])
def test_current_request_has_conservative_compatible_intent(body, kind):
    intent = build_request_intent([email(body)])
    assert intent.recommended_type == kind
    if kind != "uncertain":
        assert intent.evidence and intent.evidence[0].excerpt in body


def test_existing_project_match_does_not_classify_new_work_as_amendment():
    intent = build_request_intent([email("A new enquiry for the extension to the same site.")],
                                  matched_projects=[match()])
    assert intent.recommended_type == "new_enquiry"
    assert intent.matched_projects[0].similarity_score == 0.99
    assert reason_for_change(intent) == "New Enquiry (AI recommendation)"
    assert "probability" not in intent.model_dump()


def test_amendment_without_reference_can_have_title_based_candidates():
    intent = build_request_intent([email("Please revise the existing roof scheme.")])
    result = enrich_request_intent(intent, [match("123"), match("124")])
    assert result.recommended_type == "amendment"
    assert len(result.matched_projects) == 2
    assert result.ambiguity_reasons == ["Several projects remain plausible; the reviewer must select the relevant project."]
    assert not result.detected_references


def test_verified_current_citations_support_model_contextual_assessment():
    body = "We need the alternative construction incorporated into your original layout."
    intent = build_request_intent([email(body)], proposal("amendment", body))
    assert intent.recommended_type == "amendment"
    assert intent.evidence[0].source_id == "asset:1:body"
    assert reason_for_change(intent) == "Amendment (AI recommendation)"


@pytest.mark.parametrize("citation,source_id", [("Please revise an invented scheme", "asset:1:body"),
                                               ("Please adjust the scheme", "asset:999:body")])
def test_unverified_model_citations_remain_uncertain(citation, source_id):
    intent = build_request_intent([email("Please adjust the scheme")],
                                  proposal("amendment", citation, source_id=source_id))
    assert intent.recommended_type == "uncertain"
    assert intent.ambiguity_reasons


def test_historical_amendment_quotation_cannot_control_new_request():
    current = "This is a new enquiry for a separate extension. Reference AB00123 may help locate the site."
    old = "Please revise the existing scheme CD00123_25.01 - AA."
    source = email(current + "\n\n" + email(old, date="2 Nov 2026 10:00:00 +0000")["text"])
    intent = build_request_intent([source])
    assert intent.recommended_type == "new_enquiry"
    assert {ref.literal: ref.role for ref in intent.detected_references} == {
        "AB00123": "current_request", "CD00123_25.01 - AA": "forwarded_context",
    }
    rejected = build_request_intent([source], proposal("amendment", old))
    assert rejected.recommended_type == "uncertain"
    assert "selected external client request" in rejected.explanation


def test_latest_external_client_not_internal_forward_controls_intent():
    client = email("A new enquiry for a new building.", date="8 Nov 2026 10:00:00 +0000")["text"]
    source = email("Please change our internal tracker.\n\n" + client, sender="surveyor@taperedplus.co.uk")
    intent = build_request_intent([source])
    assert intent.recommended_type == "new_enquiry"
    assert intent.evidence[0].excerpt == "A new enquiry for a new building."


def test_drawing_reference_alone_is_search_evidence_not_amendment_proof():
    intent = build_request_intent([email("For the school site AB00123_26.01 – AA, please call me.")])
    assert intent.recommended_type == "uncertain"
    reference = intent.detected_references[0]
    assert reference.kind == "drawing"
    assert reference.literal == "AB00123_26.01 – AA"
    assert reference.literal in reference.evidence.excerpt


def test_attachment_reference_is_retained_and_unrelated_email_attachments_excluded():
    sources = [email("Please revise the scheme in the attached drawings.")]
    sources += [{"sourceId": "asset:1:attachment:1", "sourceType": "attachment", "status": "read",
                 "text": "Roof A\nDrawing AB00123_26.01 - B"},
                email("A new project.", identity="asset:2:body", date="1 Nov 2026 10:00:00 +0000"),
                {"sourceId": "asset:2:attachment:1", "sourceType": "attachment", "status": "read",
                 "text": "CD88888_25.01 - Z"}]
    intent = build_request_intent(sources)
    assert intent.recommended_type == "amendment"
    assert [(ref.literal, ref.role) for ref in intent.detected_references] == [("AB00123_26.01 - B", "document")]


def test_request_attachment_can_support_model_intent_with_generic_email_body():
    source_id = "asset:1:attachment:1"
    sources = [email("Please see the attached request."),
               {"sourceId": source_id, "sourceType": "attachment", "status": "read",
                "text": "Please amend our existing layout to suit the reduced outlet count."}]
    intent = build_request_intent(sources, proposal("amendment", sources[1]["text"], source_id=source_id))
    assert intent.recommended_type == "amendment"
    assert intent.evidence[0].source_id == source_id


def test_internal_only_or_incomplete_context_returns_usable_uncertain_result():
    intent = build_request_intent([email("Please revise scheme", sender="team@taperedplus.co.uk")])
    assert intent.recommended_type == "uncertain"
    assert reason_for_change(intent) == "Reviewer decision required"
    assert intent.ambiguity_reasons


def test_model_reported_conflict_stays_reviewable_and_matches_never_resolve_it():
    data = proposal("uncertain", "Please review the roof.", ambiguity_reasons=["Two current requests conflict."])
    intent = build_request_intent([email("Please review the roof.")], data, matched_projects=[match()])
    assert intent.recommended_type == "uncertain"
    assert intent.ambiguity_reasons == ["Two current requests conflict."]


def test_incomplete_client_headers_preserve_search_references_with_uncertain_intent():
    source = {"sourceId": "asset:1:body", "sourceType": "email_body", "status": "read",
              "text": "Subject: AB00123_26.01 - A\nPlease revise this scheme"}
    intent = build_request_intent([source])
    assert intent.recommended_type == "uncertain"
    assert intent.detected_references[0].literal == "AB00123_26.01 - A"
    assert intent.detected_references[0].role == "forwarded_context"


def test_conflicting_current_instructions_cannot_be_cleared_by_model_selection():
    body = "This is a new enquiry. Please amend the previous scheme."
    intent = build_request_intent([email(body)], proposal("amendment", body))
    assert intent.recommended_type == "uncertain"
    assert intent.evidence


def test_scoped_literal_reference_citation_is_preserved():
    body = "Please revise the scheme.\nRoof A: AB00123_26.01 - A"
    data = proposal("amendment", "Please revise the scheme.")
    data["detected_references"] = [{"kind": "drawing", "literal": "AB00123_26.01 - A",
        "evidence": {"source_id": "asset:1:body", "excerpt": "Roof A: AB00123_26.01 - A",
                     "scope": "Roof A", "location": "email body"}}]
    intent = build_request_intent([email(body)], data)
    assert intent.detected_references[0].evidence.scope == "Roof A"


def test_sidecar_is_typed_and_does_not_change_csv_or_pdf_scalar_fields():
    assert "specification_request_intent" in DesignParameterExtraction.model_fields
    assert len(CANONICAL_PARAMETER_ORDER) == 21
    assert CANONICAL_PARAMETER_ORDER.count("Reason for Change") == 1
    assert "specification_request_intent" not in CANONICAL_PARAMETER_ORDER
    assert "selected external client's current request" in PARAMETER_EXTRACTION_PROMPT
    assert "Reason for Change" not in PARAMETER_EXTRACTION_PROMPT
    with pytest.raises(ValueError, match="supporting email context"):
        RequestIntentRecommendation(recommended_type="amendment", explanation="Same project", matched_projects=[match()])


def test_gemini_extraction_carries_and_verifies_typed_intent_sidecar():
    from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
    from backend.app.services.specification_semantics import FIELD_DEFINITIONS

    body = "Please adjust the existing scheme for the increased outlet count."
    payload = {name: None for name in DesignParameterExtraction.model_fields if not name.startswith("specification_")}
    payload.update(specification_evidence=[], specification_assessed_fields=list(FIELD_DEFINITIONS),
                   specification_request_intent=proposal("amendment", body))
    schemas = []

    def generate(model, contents, config):
        schemas.append(config.response_json_schema)
        return SimpleNamespace(text=json.dumps(payload), parsed=None)

    result = LegacyGeminiClient("offline", generate_content=generate).extract_design_parameters(
        body, source_manifest=[email(body)])
    assert result.specification_request_intent.recommended_type == "amendment"
    assert result.specification_request_intent.evidence[0].excerpt == body
    assert "specification_request_intent" in schemas[0]["properties"]
