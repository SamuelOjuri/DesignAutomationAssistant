"""Business contract examples; live source resolution is a later phase."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.services.performance_specification_contract import CADSpecificationValues, FIELD_NAMES
from backend.app.services.specification_review_contract import (
    AutomaticProposal, CADMapping, CandidateLookup, FIELD_POLICY, REVIEW_FIELD_DEFINITIONS,
    ReviewDecisionState, ReviewSelections, ReviewSourceSnapshot,
    RequestIntentRecommendation, SaveReviewDecisions, SavedFieldDecision, SourceIdentity,
    effective_field_value,
)


SCENARIOS = json.loads((Path(__file__).parent / "fixtures/specification_review/scenarios.json").read_text(encoding="utf-8"))
IDENTITY = SourceIdentity(source_fingerprint="sources-1", extraction_identity="extraction-1",
    source_policy_version="specification-review-source-v1", reference_parser_version="fixture-parser-v1")
AUDIT = {
    "actor": {"app_user_id": "reviewer-1", "monday_user_id": "100", "monday_account_id": "200"},
    "saved_at": "2026-11-09T10:00:00+00:00", "reviewed_source": IDENTITY.model_dump(),
}
EMAIL = {"source_id": "request:body", "excerpt": "Requested build-up", "location": "client email body"}
CRM = {"record": {"board_id": "70002", "item_id": "82001"}, "column_id": "build_up", "scope": "Roof 001"}


def proposal(value="Concrete", source="email_context", **changes):
    payload = {"proposal_id": "proposal-1", "display_value": value, "source": source,
        "rationale": "Applicable selected request/roof", "cad_mapping": {"status": "unmapped"}}
    if source == "email_context":
        payload["email_evidence"] = [EMAIL]
    elif source == "monday_crm":
        payload["crm_evidence"] = [CRM]
    elif source == "derived":
        payload.update(crm_evidence=[CRM], derived_from="Drawing Reference")
    else:
        payload["cad_mapping"] = {"status": "not_applicable"}
    return AutomaticProposal.model_validate({**payload, **changes})


def saved_decision(operation, **values):
    return SavedFieldDecision.model_validate({"decision": {"operation": operation, **values}, "audit": AUDIT})


def command(**changes):
    return {"expected_review_version": 0, "expected_source_fingerprint": "sources-1",
        "idempotency_key": "command-1", **changes}


def fixture_snapshot(scenario):
    """Adapt golden declarations to the schema; this is not a source resolver."""
    context = scenario["context"]
    fields = {}
    for label, expected in scenario["expected"].items():
        source = expected["source"]
        fields[label] = {
            "proposal": proposal(expected["display_value"], expected["source"]).model_dump(),
            "email_facts": ([{"value": scenario["email_values"][label], "evidence": [EMAIL]}]
                if label in scenario["email_values"] else []),
            "crm_recommendations": ([{"value": scenario["crm_values"][label], "evidence": [CRM],
                "rationale": "Scoped CRM recommendation"}] if label in scenario["crm_values"] else []),
            "issues": [{"code": code, "detail": code.replace("_", " ")} for code in expected.get("issues", [])],
            "applicability": ("unassigned" if source == "unassigned" else "not_applicable" if source == "default_blank"
                else "unresolved" if source == "unavailable" else "applicable"),
            "scope": context.get("roof_scope"),
        }
    candidates = {"status": "available", "drawings": [{
        "record": roof["drawing"], "project": roof["project"], "literal_reference": roof["drawing_reference"],
        "roof_scope": roof["roof_scope"], "lifecycle": "active", "match_kind": "latest_in_scope", "rationale": "Plausible existing roof",
    } for roof in context.get("ambiguous_roofs", [])]}
    return {
        "identity": IDENTITY.model_dump(),
        "intent": {"recommended_type": context["intent"]["recommended_type"],
            "explanation": context["intent"]["rationale"], "evidence": context["intent"]["evidence"],
            "ambiguity_reasons": context["intent"].get("ambiguity_reasons", [])},
        "request_type": None if scenario["request_type"] == "uncertain" else scenario["request_type"],
        "candidates": candidates,
        "fields": fields,
    }


def test_policy_preserves_all_pdf_labels_and_new_enquiry_identity_defaults():
    assert tuple(FIELD_POLICY) == tuple(REVIEW_FIELD_DEFINITIONS) == FIELD_NAMES
    assert [label for label in FIELD_NAMES if FIELD_POLICY[label].unassigned_permitted] == ["Drawing Reference", "Revision"]
    for label in FIELD_NAMES:
        sources = FIELD_POLICY[label].sources_for("new_enquiry")
        assert "monday_crm" not in sources  # A historical project never supplies a new scheme's technical values.
        if label in {"Drawing Reference", "Revision"}:
            assert sources == ("unassigned",)
        elif label == "Incoming Information":
            assert sources == ("default_blank",)
        else:
            assert sources == ("email_context",)


def test_amendment_matrix_encodes_email_first_fallback_and_account_exception():
    for label in ("Contact", "Surveyor", "Target U Value", "Target Min U Value", "Membrane", "Tapered Insulation", "VCL", "Decking"):
        assert FIELD_POLICY[label].sources_for("amendment") == ("email_context", "monday_crm")
    assert FIELD_POLICY["Company"].sources_for("amendment") == ("monday_crm",)
    assert FIELD_POLICY["Drawing Reference"].sources_for("amendment") == ("email_context", "monday_crm")
    assert FIELD_POLICY["Revision"].sources_for("amendment") == ("derived",)
    for label in ("Scale", "Page Size", "Drawing Title", "Date Received", "Bauder Contract Number", "Bauder Roof Num"):
        assert FIELD_POLICY[label].sources_for("amendment") == ("email_context",)
    with pytest.raises(ValueError, match="classification"):
        FIELD_POLICY["Company"].sources_for("uncertain")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case["id"])
def test_golden_source_outputs_fit_contract_and_declared_precedence(scenario):
    """Pin policy examples now so future resolvers can run against the same inputs."""
    assert tuple(scenario["expected"]) == FIELD_NAMES
    assert set(scenario["email_values"]) <= set(FIELD_NAMES)
    assert set(scenario["crm_values"]) <= set(FIELD_NAMES)
    snapshot = ReviewSourceSnapshot.model_validate(fixture_snapshot(scenario))
    assert snapshot.intent.recommended_type == scenario["request_type"]
    for label, expected in scenario["expected"].items():
        source, value = expected["source"], expected["display_value"]
        if source == "email_context":
            assert value == scenario["email_values"][label]
        elif source == "monday_crm":
            assert value == scenario["crm_values"][label]
            if label != "Company":
                assert label not in scenario["email_values"]
        elif source == "derived":
            assert label == "Revision"
            assert value == scenario["context"]["selected_drawing_reference"].split("_", 1)[1]
        elif source in {"unassigned", "unavailable"}:
            assert value is None
        else:
            assert label == "Incoming Information" and value == ""
        effective = effective_field_value(snapshot.fields[label].proposal, current_source=IDENTITY)
        assert effective.display_value == value
        assert effective.source == source
    assert snapshot.fields["Date Received"].proposal.display_value != scenario["context"]["holding_values"]["Date Received"]


def test_account_disagreement_retains_email_crm_and_review_issue():
    case = next(case for case in SCENARIOS if case["id"] == "amendment_email_precedence")
    field = ReviewSourceSnapshot.model_validate(fixture_snapshot(case)).fields["Company"]
    assert field.email_facts[0].value == "Example Construction Trading Name"
    assert field.crm_recommendations[0].value == field.proposal.display_value == "Example Construction Ltd"
    assert field.issues[0].code == "company_account_disagreement"


def test_ambiguous_roof_fixture_retains_distinct_candidates_and_no_technical_merge():
    case = next(case for case in SCENARIOS if case["id"] == "uncertain_multiple_roofs")
    assert len(case["context"]["ambiguous_roofs"]) > 1
    for label in ("Drawing Reference", "Revision", "Target U Value", "Membrane", "Tapered Insulation", "VCL", "Decking"):
        assert case["expected"][label]["source"] == "unavailable"
    snapshot = ReviewSourceSnapshot.model_validate(fixture_snapshot(case))
    assert [drawing.roof_scope for drawing in snapshot.candidates.drawings] == ["North roof", "South roof"]
    assert snapshot.fields["Decking"].applicability == "unresolved"
    assert snapshot.intent.ambiguity_reasons


def test_unavailable_candidate_lookup_remains_distinct_from_valid_empty_inventory():
    empty = CandidateLookup(status="available")
    failed = CandidateLookup(status="unavailable", issues=["Project board could not be read"])
    assert not empty.drawings and not failed.drawings
    assert empty.status != failed.status


@pytest.mark.parametrize("label", FIELD_NAMES)
@pytest.mark.parametrize("operation", ["specify_value", "specify_blank", "accept_recommendation", "restore_recommendation"])
def test_every_field_accepts_each_reviewer_command(label, operation):
    values = {"display_value": "Reviewer text"} if operation == "specify_value" else {"proposal_id": "p1"} if operation == "accept_recommendation" else {}
    parsed = SaveReviewDecisions.model_validate(command(field_operations={label: {"operation": operation, **values}}))
    assert parsed.field_operations[label].operation == operation


@pytest.mark.parametrize("label", FIELD_NAMES)
def test_all_fields_preserve_custom_display_and_intentional_blank(label):
    # Includes Incoming Information, Revision, numeric fields and CAD dropdowns.
    literal = "  R\u00e9viewer custom text\nsecond line  "
    saved = saved_decision("specify_value", display_value=literal)
    state = ReviewDecisionState(field_decisions={label: saved})
    result = effective_field_value(proposal(), state.field_decisions[label], current_source=IDENTITY)
    assert result.display_value == literal
    assert result.source_label == "Reviewer Specified"
    assert result.cad_mapping.status == "unmapped"
    blank = effective_field_value(proposal(), saved_decision("specify_blank"), current_source=IDENTITY)
    assert blank.display_value == ""  # Never replaced by the populated proposal.
    assert blank.source_label == "Reviewer Specified"
    assert blank.cad_mapping.status == "not_applicable"


@pytest.mark.parametrize("source", ["email_context", "monday_crm"])
def test_acceptance_snapshots_value_and_actual_source_while_same_text_entry_is_reviewer_specified(source):
    old = proposal("Concrete", source)
    saved = saved_decision("accept_recommendation", snapshot=old.model_dump())
    current = proposal("Metal", source, proposal_id="p2")
    result = effective_field_value(current, saved, current_source=IDENTITY)
    assert result.display_value == "Concrete"
    assert result.source == source and result.reviewer_accepted
    manual = effective_field_value(old, saved_decision("specify_value", display_value="Concrete"), current_source=IDENTITY)
    assert manual.source_label == "Reviewer Specified" and not manual.reviewer_accepted
    # Restore removes the saved decision and uses the current proposal.
    restored = effective_field_value(current, current_source=IDENTITY)
    assert restored.display_value == "Metal" and not restored.reviewer_accepted


@pytest.mark.parametrize("operation,values", [
    ("specify_value", {"display_value": "Concrete"}), ("specify_blank", {}),
    ("accept_recommendation", {"snapshot": proposal().model_dump()}),
])
def test_relevant_source_change_flags_reconfirmation_without_losing_decision(operation, values):
    saved = saved_decision(operation, **values)
    before = saved.model_dump()
    current_source = IDENTITY.model_copy(update={"source_fingerprint": "sources-2"})
    result = effective_field_value(proposal("Metal"), saved, current_source=current_source)
    assert result.requires_reconfirmation
    assert result.display_value == ("" if operation == "specify_blank" else "Concrete")
    assert saved.model_dump() == before


def test_unread_optional_crm_issue_remains_visible_after_authoritative_override():
    payload = fixture_snapshot(SCENARIOS[0])
    payload["source_issues"] = [{"code": "source_unavailable", "source_id": "crm:optional", "detail": "Optional drawing read failed"}]
    snapshot = ReviewSourceSnapshot.model_validate(payload)
    result = effective_field_value(snapshot.fields["Decking"].proposal,
        saved_decision("specify_value", display_value="Timber (18mm)"), current_source=IDENTITY)
    assert result.source_label == "Reviewer Specified"
    assert snapshot.source_issues[0].source_id == "crm:optional"
    reviewed = SaveReviewDecisions.model_validate(command(source_dispositions=[{
        "source_id": "crm:optional", "issue_code": "source_unavailable", "explanation": "Reviewed independently for this roof",
    }]))
    assert reviewed.source_dispositions[0].explanation


@pytest.mark.parametrize("extra", [
    {"source": "reviewer_specified"}, {"actor": AUDIT["actor"]}, {"snapshot": proposal().model_dump()},
    {"cad_mapping": {"status": "mapped", "value": "Concrete", "catalogue_version": "made-up"}},
])
def test_untrusted_commands_cannot_author_provenance_or_accepted_values(extra):
    with pytest.raises(ValidationError):
        SaveReviewDecisions.model_validate(command(field_operations={"Decking": {
            "operation": "accept_recommendation", "proposal_id": "p1", **extra,
        }}))


def test_ai_recommendation_and_reviewer_selection_are_separate():
    recommendation = RequestIntentRecommendation(recommended_type="uncertain", explanation="Conflicting request context",
        ambiguity_reasons=["Multiple projects and roofs"])
    assert recommendation.recommended_type == "uncertain"
    assert ReviewDecisionState().selections.selected_request_type is None
    assert ReviewSelections(selected_request_type="amendment").selected_request_type == "amendment"
    with pytest.raises(ValidationError):
        ReviewSelections(selected_request_type="uncertain")
    with pytest.raises(ValidationError, match="supporting email"):
        RequestIntentRecommendation(recommended_type="amendment", explanation="Familiar project name",
            matched_projects=[{"record": {"board_id": "70001", "item_id": "81001"}, "rationale": "Name match", "similarity_score": 0.99}])


def test_reviewer_request_type_drives_recommendation_branch_separately_from_ai_intent():
    case = next(case for case in SCENARIOS if case["id"] == "amendment_missing_details")
    payload = fixture_snapshot(case)
    payload["intent"]["recommended_type"] = "new_enquiry"
    snapshot = ReviewSourceSnapshot.model_validate(payload)
    assert snapshot.request_type == "amendment"
    assert snapshot.fields["Decking"].proposal.source == "monday_crm"


def test_selection_patch_preserves_omitted_vs_explicit_null_and_distinct_monday_ids():
    omitted = SaveReviewDecisions.model_validate(command(selections={}))
    cleared = SaveReviewDecisions.model_validate(command(selections={"selected_drawing": None}))
    assert not omitted.selections.model_fields_set
    assert cleared.selections.model_fields_set == {"selected_drawing"}
    # A patch may select a drawing while retaining a project already saved.
    SaveReviewDecisions.model_validate(command(selections={"selected_drawing": {"board_id": "1", "item_id": "2"}}))
    with pytest.raises(ValidationError, match="project selection"):
        ReviewSelections(selected_drawing={"board_id": "1", "item_id": "2"})
    with pytest.raises(ValidationError):
        ReviewSelections(selected_project={"board_id": "1", "item_id": "AB00123"})


@pytest.mark.parametrize("change", [
    {"expected_review_version": -1}, {"expected_review_version": True}, {"expected_source_fingerprint": " "},
    {"idempotency_key": ""}, {"schema_version": 2}, {"field_operations": {"Target U-Value": {"operation": "specify_blank"}}},
    {"field_operations": {"Decking": {"operation": "specify_value", "display_value": " "}}},
    {"field_operations": {"Decking": {"operation": "specify_blank", "display_value": "Concrete"}}},
])
def test_malformed_or_ambiguous_commands_are_rejected(change):
    with pytest.raises(ValidationError):
        SaveReviewDecisions.model_validate(command(**change))


def test_restore_is_not_a_persisted_decision_and_audit_requires_aware_server_timestamp():
    with pytest.raises(ValidationError):
        saved_decision("restore_recommendation")
    with pytest.raises(ValidationError):
        SavedFieldDecision.model_validate({"decision": {"operation": "specify_blank"},
            "audit": {**AUDIT, "saved_at": "2026-11-09T10:00:00"}})


@pytest.mark.parametrize("source,evidence_key", [("email_context", "email_evidence"), ("monday_crm", "crm_evidence")])
def test_populated_recommendations_require_actual_source_identity(source, evidence_key):
    payload = proposal(source=source).model_dump()
    payload[evidence_key] = []
    with pytest.raises(ValidationError):
        AutomaticProposal.model_validate(payload)


def test_unavailable_is_distinct_from_default_blank_and_unassigned_and_cannot_be_accepted():
    for source, value in (("unavailable", None), ("unassigned", None), ("default_blank", "")):
        result = proposal(value, source)
        assert result.source == source and result.display_value == value
    with pytest.raises(ValidationError):
        saved_decision("accept_recommendation", snapshot=proposal(None, "unavailable").model_dump())
    with pytest.raises(ValidationError):
        proposal("Not Provided", "unassigned")


def test_source_snapshot_requires_all_fields_and_field_appropriate_origins():
    payload = fixture_snapshot(SCENARIOS[0])
    del payload["fields"]["Revision"]
    with pytest.raises(ValidationError, match="18 exact PDF labels"):
        ReviewSourceSnapshot.model_validate(payload)
    payload = fixture_snapshot(SCENARIOS[0])
    payload["fields"]["Decking"]["proposal"] = proposal("26.01 - A", "derived").model_dump()
    with pytest.raises(ValidationError, match="outside the field policy"):
        ReviewSourceSnapshot.model_validate(payload)
    payload = fixture_snapshot(SCENARIOS[0])
    payload["fields"]["Decking"]["proposal"] = proposal("Metal", "monday_crm").model_dump()
    with pytest.raises(ValidationError, match="outside the field policy"):
        ReviewSourceSnapshot.model_validate(payload)


def test_mappings_require_authoritative_version_without_discarding_custom_text():
    with pytest.raises(ValidationError):
        CADMapping(status="mapped", value="Concrete")
    with pytest.raises(ValidationError):
        CADMapping(status="unmapped", target_identity="invented-trics-id")
    saved = saved_decision("specify_value", display_value="Concrete as described by reviewer",
        cad_mapping={"status": "mapped", "value": "Concrete", "catalogue_version": "test-catalogue-v1"})
    value = effective_field_value(proposal(), saved, current_source=IDENTITY)
    assert value.display_value == "Concrete as described by reviewer"
    assert value.cad_mapping.value == "Concrete"
    value.cad_mapping.value = "Metal"
    assert saved.decision.cad_mapping.value == "Concrete"  # Presentation cannot mutate the audit snapshot.


def test_automated_cad_contract_stays_separate_from_reviewer_display():
    values = {label: None for label in FIELD_NAMES}
    values["Incoming Information"] = "Reviewer text"
    with pytest.raises(ValidationError):
        CADSpecificationValues.model_validate(values)
    values["Incoming Information"] = ""
    values["Decking"] = "Custom deck specification"
    with pytest.raises(ValidationError):
        CADSpecificationValues.model_validate(values)


def test_schema_can_be_exported_for_future_persistence_and_mocked_frontend():
    for model in (ReviewSourceSnapshot, ReviewDecisionState, SaveReviewDecisions):
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert "Reviewer Specified" not in json.dumps(SaveReviewDecisions.model_json_schema())
    before = fixture_snapshot(SCENARIOS[0])
    snapshot = ReviewSourceSnapshot.model_validate(deepcopy(before))
    assert ReviewSourceSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
