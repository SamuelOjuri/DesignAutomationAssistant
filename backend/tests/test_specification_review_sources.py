"""Phase 4 field policy, live source guards and PDF-only decision resolution."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from backend.app.services.performance_specification_sources import (
    SpecificationReviewRequired, SpecificationSourceError, resolve_specification_sources,
)
from backend.app.services.specification_review_contract import (
    FIELD_NAMES, ReviewActor, ReviewDecisionState, ReviewSourceSnapshot, SavedFieldDecision,
)
from backend.tests.test_performance_specification import case, column, with_evidence


def source(case, state=None):
    return resolve_specification_sources(case.gateway, case.item, review_state=state)


def saved_state(case, label, operation, **values):
    current = ReviewSourceSnapshot.model_validate(source(case)["reviewSource"])
    state = ReviewDecisionState(review_version=1)
    state.field_decisions[label] = SavedFieldDecision.model_validate({
        "decision": {"operation": operation, **values},
        "audit": {"actor": ReviewActor(app_user_id="reviewer", monday_user_id="1", monday_account_id="2"),
            "saved_at": datetime.now(timezone.utc), "reviewed_source": current.identity},
    })
    return state


def remove_email(case, *labels):
    extraction = deepcopy(case.item.extracted_parameters_json)
    for label in labels:
        extraction["performanceParameters"][label] = None
        extraction["performanceParameters"][label.replace("U Value", "U-Value")] = None
    case.item.extracted_parameters_json = with_evidence(extraction)


def test_amendment_uses_current_email_before_selected_drawing_values(case):
    case.gateway.project["subitems"][0]["column_values"].extend([
        column("contact", "Contact", "Old Contact"), column("deck", "Decking", "Timber"),
    ])
    result = source(case)
    assert result["raw"]["Target U Value"] == "0.18"
    assert result["raw"]["Contact"] == "Alex Example"
    assert result["raw"]["Decking"] == "Concrete"
    assert result["raw"]["Drawing Title"] == "Old title"
    assert result["sources"]["Target U Value"]["source"] == "Email Context"


@pytest.mark.parametrize("label,crm_value", [
    ("Target U Value", "0.22"), ("Target Min U Value", "0.25"),
    ("Membrane", "Single Ply"), ("Tapered Insulation", "TR27"), ("VCL", "Polythene AVCL"),
    ("Decking", "Timber"), ("Contact", "Selected Requester"), ("Surveyor", "Selected Surveyor"),
])
def test_missing_amendment_fields_use_only_selected_drawing_recommendations(case, label, crm_value):
    remove_email(case, label)
    case.gateway.project["subitems"][0]["column_values"] = [column("selected", label, crm_value)]
    result = source(case)
    snapshot = ReviewSourceSnapshot.model_validate(result["reviewSource"])
    assert result["raw"][label] == crm_value
    assert snapshot.fields[label].proposal.source == "monday_crm"
    assert snapshot.fields[label].proposal.crm_evidence[0].record.item_id == "9101"


def test_new_enquiry_does_not_import_old_project_or_holding_business_values(case):
    extraction = deepcopy(case.item.extracted_parameters_json)
    extraction["specificationRequestIntent"]["recommended_type"] = "new_enquiry"
    extraction["performanceParameters"]["Company"] = "Email company"
    case.item.extracted_parameters_json = with_evidence(extraction)
    case.gateway.fetch_project = lambda *args: pytest.fail("A new enquiry imported an old project")
    before = source(case)
    assert before["raw"]["Drawing Reference"] is None
    assert before["raw"]["Revision"] is None
    assert before["raw"]["Incoming Information"] == ""
    assert before["raw"]["Company"] == "Email company"
    assert before["raw"]["Date Received"] != "2026-10-30"
    case.gateway.holding["column_values"] = [column("unrelated", "Unrelated", "ignored")]
    assert source(case) == before


def test_company_account_disagreement_retains_both_values_for_reconciliation(case):
    extraction = deepcopy(case.item.extracted_parameters_json)
    extraction["performanceParameters"]["Company"] = "Email company"
    case.item.extracted_parameters_json = with_evidence(extraction)
    result = source(case)
    field = result["reviewSource"]["fields"]["Company"]
    assert result["raw"]["Company"] == "Acme, Limited"
    assert field["email_facts"][0]["value"] == "Email company"
    assert field["crm_recommendations"][0]["value"] == "Acme, Limited"
    assert field["issues"][0]["code"] == "company_disagreement"


def test_uncertain_intent_is_not_classified_from_project_matches_or_holding_relation(case):
    extraction = deepcopy(case.item.extracted_parameters_json)
    extraction["specificationRequestIntent"]["recommended_type"] = "uncertain"
    case.item.extracted_parameters_json = extraction
    case.gateway.fetch_project = lambda *args: pytest.fail("Uncertain intent imported amendment technical data")
    result = source(case)
    assert result["reviewSource"]["request_type"] is None
    assert result["reviewSource"]["selected_project"] is None
    assert result["raw"]["Target U Value"] == "0.18"


@pytest.mark.parametrize("label", FIELD_NAMES)
@pytest.mark.parametrize("operation,value", [("specify_value", "Reviewer’s exact custom value"), ("specify_blank", "")])
def test_all_18_fields_allow_authoritative_text_and_intentional_blank(case, label, operation, value):
    state = saved_state(case, label, operation, **({"display_value": value} if value else {}))
    result = source(case, state)
    assert result["raw"][label] == value
    assert result["sources"][label]["source"] == "Reviewer Specified"
    assert label not in result["issues"]
    assert result["reviewSource"]["fields"][label]["proposal"]["source"] != "reviewer_specified"


def test_acceptance_snapshots_recommendation_and_restore_recomputes(case):
    snapshot = ReviewSourceSnapshot.model_validate(source(case)["reviewSource"])
    state = saved_state(case, "Decking", "accept_recommendation", snapshot=snapshot.fields["Decking"].proposal)
    extraction = deepcopy(case.item.extracted_parameters_json)
    extraction["performanceParameters"]["Decking"] = "Timber"
    case.item.extracted_parameters_json = with_evidence(extraction)
    accepted = source(case, state)
    assert accepted["raw"]["Decking"] == "Concrete"
    assert accepted["sources"]["Decking"]["source"] == "Email Context"
    assert source(case)["raw"]["Decking"] == "Timber"


def test_explicit_drawing_selection_describes_that_existing_revision_and_retains_email_target(case):
    chosen = deepcopy(case.gateway.project["subitems"][0])
    chosen.update(id="9102", name="TP00901_26.02 - AA")
    case.gateway.project["subitems"].append(chosen)
    state = ReviewDecisionState(selections={"selected_request_type": "amendment",
        "selected_project": {"board_id": case.gateway.project["board"]["id"], "item_id": "901"},
        "selected_drawing": {"board_id": "9000", "item_id": "9102"}})
    result = source(case, state)
    assert result["raw"]["Drawing Reference"] == "TP00901_26.02 - AA"
    assert result["raw"]["Revision"] == "26.02 - AA"
    fields = result["reviewSource"]["fields"]
    assert fields["Drawing Reference"]["email_facts"][0]["value"] == "TP00901_26.01 - A"
    assert fields["Drawing Reference"]["issues"][0]["code"] == "drawing_reference_disagreement"


def test_unreadable_optional_crm_does_not_block_manual_field_but_keeps_failure_audit(case):
    remove_email(case, "Contact")
    state = saved_state(case, "Contact", "specify_value", display_value="Verified Reviewer Contact")
    case.gateway.project["subitems"][0]["column_values"].append({
        "id": "mirror_11__1", "type": "mirror", "column": {"title": "Contact"},
        "display_value": "Unresolved person", "mirrored_items": [],
    })
    result = source(case, state)
    assert result["raw"]["Contact"] == "Verified Reviewer Contact"
    assert "Contact" not in result["issues"]
    assert result["reviewSource"]["fields"]["Contact"]["issues"][0]["code"] == "crm_recommendation_unavailable"
    assert any(read["field"] == "Contact" and read["status"] == "unavailable" for read in result["crmReads"])
    case.gateway.target.input_revision = "changed"
    with pytest.raises(SpecificationSourceError, match="current email extraction"):
        source(case, state)


def test_relevant_source_hash_changes_identity_but_unrelated_crm_fields_do_not(case):
    original = source(case)
    case.gateway.project["column_values"].append(column("unrelated", "Ignored CRM field", "changed"))
    assert source(case) == original
    case.item.extracted_parameters_json = deepcopy(case.item.extracted_parameters_json)
    case.item.extracted_parameters_json["sourceManifest"][0]["contentSha256"] = "changed"
    changed = source(case)
    assert changed["reviewSource"]["identity"] != original["reviewSource"]["identity"]


def test_unverified_email_can_be_resolved_only_by_a_decision_for_that_field(case):
    state = saved_state(case, "Decking", "specify_blank")
    extraction = deepcopy(case.item.extracted_parameters_json)
    next(entry for entry in extraction["specificationEvidence"] if entry["field"] == "Decking")["snippet"] = "Fabricated excerpt"
    case.item.extracted_parameters_json = extraction
    with pytest.raises(SpecificationReviewRequired):
        source(case)
    assert source(case, state)["raw"]["Decking"] == ""
