"""Drawing identity regressions with synthetic email and CRM evidence only."""

from copy import deepcopy

import pytest

from backend.app.models import PerformanceSpecificationArtifact
from backend.app.services.performance_specification_evidence import REVIEW_PREFIX_REFERENCE
from backend.app.services.performance_specification_sources import resolve_specification_sources
from backend.tests.test_performance_specification import (
    BOARD, ITEM, case, column, current, pdf_text, relation, run,
)


def without_requested_reference(case):
    data = deepcopy(case.item.extracted_parameters_json)
    data["performanceParameters"]["Drawing Reference"] = None
    data["specificationEvidence"] = [e for e in data["specificationEvidence"] if e["field"] != "Drawing Reference"]
    case.item.extracted_parameters_json = data
    return data


def add_drawing(case, reference="TP00901_26.02 - A", identity="9102"):
    drawing = deepcopy(case.gateway.project["subitems"][0])
    drawing.update(id=identity, name=reference)
    case.gateway.project["subitems"].append(drawing)
    return drawing


def add_baseline(data, reference="TP00901_26.01 - A", **overrides):
    snippet = f"Existing drawing: {reference}"
    data["sourceManifest"][0]["text"] += "\n" + snippet
    evidence = {
        "field": "Drawing Reference", "value": reference, "source_id": "email:body",
        "snippet": snippet, "location": "quoted reply", "scope": None, "issuer": "TaperedPlus",
        "role": "baseline", "request_role": "forwarded_request", "message_date": None,
        "rationale": "Existing scheme being amended", **overrides,
    }
    data["specificationEvidence"].append(evidence)
    return evidence


def artifact(case):
    return case.db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, current(case).current_revision))


def test_verified_baseline_selects_drawing_without_promoting_historical_specs(case):
    data = without_requested_reference(case)
    add_baseline(data)
    add_drawing(case)
    # The unselected drawing has unusable mirrors; only the identified one is read.
    case.gateway.project["subitems"][1]["column_values"] = None
    case.gateway.project["subitems"][0]["column_values"] = []
    for evidence in data["specificationEvidence"]:
        if evidence["field"] in {"Target U Value", "Tapered Insulation"}:
            evidence["role"] = "achieved" if evidence["field"] == "Target U Value" else "baseline"
    data["performanceParameters"].update({"Target U-Value": None, "Tapered Insulation": None})
    note = "Keep the tapered gutter below 70mm."
    data["sourceManifest"][0]["text"] += "\n" + note
    data["specificationRequestNotes"] = [{"source_id": "email:body", "snippet": note,
                                         "scope": None, "category": "constraint"}]
    assert run(case) == ["published"]
    source = current(case).source_json
    assert source["drawingItemId"] == "9101"
    assert source["raw"]["Drawing Reference"] == "TP00901_26.01 - A"
    assert source["raw"]["Revision"] == "26.01 - A"
    assert source["raw"]["Target U Value"] is None
    assert source["raw"]["Tapered Insulation"] is None
    assert source["drawingSelection"]["evidence"][0]["role"] == "baseline"
    assert artifact(case).source_json["drawingSelection"] == source["drawingSelection"]
    assert note in pdf_text(case.gateway.uploads[0][1])


@pytest.mark.parametrize("change", [
    {"snippet": "A passage that the email does not contain"},
    {"value": "TP00901_26.09 - Z"},
    {"source_id": "not-read"},
    {"message_date": "1 January 1999 09:00"},
    {"issuer": "Unquoted issuer"},
])
def test_unverified_baseline_identity_still_blocks(case, change):
    data = without_requested_reference(case)
    evidence = add_baseline(data)
    evidence.update(change)
    add_drawing(case)
    assert run(case) == ["failed"]
    assert current(case).review_json["status"] == "conflict"
    assert not case.gateway.uploads


def test_several_baseline_drawing_identities_are_not_resolved_by_date(case):
    data = without_requested_reference(case)
    add_baseline(data, message_date="29 October 2026 12:00")
    add_baseline(data, "TP00901_26.02 - A", message_date="30 October 2026 12:00")
    data["sourceManifest"][0]["text"] += "\n29 October 2026 12:00\n30 October 2026 12:00"
    add_drawing(case)
    assert run(case) == ["failed"]
    assert "Conflicting" in current(case).review_json["issues"]["Drawing Reference"]
    assert not case.gateway.uploads


@pytest.mark.parametrize("drawings", ["different_series", "duplicate", "none"])
def test_known_baseline_without_unique_crm_match_does_not_use_unassigned_fallback(case, drawings):
    add_baseline(without_requested_reference(case))
    if drawings == "different_series":
        case.gateway.project["subitems"][0]["name"] = "TP00901_26.02 - A"
    elif drawings == "duplicate":
        add_drawing(case, "TP00901_26.01 - A")
    else:
        case.gateway.project["subitems"] = []
    assert run(case) == ["failed"]
    assert not case.gateway.uploads


def test_requested_reference_takes_precedence_over_baseline(case):
    data = deepcopy(case.item.extracted_parameters_json)
    add_baseline(data, "TP00901_26.02 - A")
    case.item.extracted_parameters_json = data
    add_drawing(case)
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["drawingItemId"] == "9101"


def test_missing_reference_publishes_email_values_and_review_without_reading_rollups(case):
    data = without_requested_reference(case)
    add_baseline(data, "RLTF-002", issuer="Bauder")
    add_drawing(case)
    for drawing in case.gateway.project["subitems"]:
        drawing["column_values"] = None  # Drawing fields are outside the confirmed scope.
    case.gateway.project["column_values"].append({
        "id": "u_value_rollup", "column": {"title": "Target U Value"}, "type": "mirror",
        "display_value": "0.20, 0.34", "mirrored_items": [],
    })
    assert run(case) == ["published"]
    record, published = current(case), artifact(case)
    assert record.review_json["status"] == "published_with_review"
    assert published.translation_json["values"]["Drawing Reference"] is None
    assert published.translation_json["values"]["Revision"] is None
    assert published.translation_json["values"]["Target U Value"] == "0.18"
    assert published.translation_json["values"]["Contact"] == "Alex Example"
    assert record.source_json["drawingItemId"] is None
    assert len(record.source_json["drawingSelection"]["candidates"]) == 2
    assert all(read["itemId"] in {"901", ITEM} for read in record.source_json["crmReads"])
    text = pdf_text(case.gateway.uploads[0][1])
    assert "Confirm the drawing reference" in text
    assert "drawing-specific CRM values have not been used" in text
    calls = len(case.calls)
    assert run(case) == ["unchanged"]
    assert len(case.calls) == calls and len(case.gateway.uploads) == 1


def test_holding_reference_resolves_unassigned_publication_on_next_refresh(case):
    without_requested_reference(case)
    add_drawing(case)
    assert run(case) == ["published"]
    previous_revision = current(case).current_revision
    case.gateway.holding["column_values"].append(column("drawing_reference", "Drawing Reference", "TP00901_26.02 - A"))
    assert run(case) == ["published"]
    assert current(case).current_revision != previous_revision
    assert current(case).source_json["drawingItemId"] == "9102"
    assert current(case).review_json is None
    assert len(case.gateway.uploads) == 2 and len(case.gateway.deletes) == 1


def test_conflicting_requested_references_still_block(case):
    data = deepcopy(case.item.extracted_parameters_json)
    evidence = add_baseline(data, "TP00901_26.02 - A", role="requested", request_role="direct_request")
    evidence["scope"] = "Other roof"
    data["performanceParameters"]["Drawing Reference"] = None
    case.item.extracted_parameters_json = data
    add_drawing(case)
    assert run(case) == ["failed"]
    assert not case.gateway.uploads


FA_REFERENCE = "FA18910_26.01 - A"


def requested_review_prefix_reference(case):
    data = deepcopy(case.item.extracted_parameters_json)
    data["performanceParameters"]["Drawing Reference"] = FA_REFERENCE
    for evidence in data["specificationEvidence"]:
        if evidence["field"] == "Drawing Reference":
            evidence.update(value=FA_REFERENCE, snippet=f"Reference: {FA_REFERENCE}", issuer=None)
    data["sourceManifest"][0]["text"] += f"\nReference: {FA_REFERENCE}"
    case.item.extracted_parameters_json = data


@pytest.mark.parametrize("role", ["baseline", "requested"])
def test_review_prefix_reference_selects_its_crm_drawing_and_publishes_for_review(case, role):
    # Render job 246c1170: FA-prefixed references are accepted, never as verified TP drawings.
    if role == "baseline":
        add_baseline(without_requested_reference(case), FA_REFERENCE, issuer=None)
    else:
        requested_review_prefix_reference(case)
    case.gateway.project["subitems"][0]["name"] = FA_REFERENCE
    assert run(case) == ["published"]
    record, published = current(case), artifact(case)
    assert record.review_json["status"] == "published_with_review"
    assert record.source_json["drawingItemId"] == "9101"
    assert record.source_json["raw"]["Drawing Reference"] == FA_REFERENCE
    assert record.source_json["raw"]["Revision"] == "26.01 - A"
    assert record.source_json["issues"] == {"Drawing Reference": REVIEW_PREFIX_REFERENCE}
    assert published.translation_json["values"]["Drawing Reference"] is None
    text = pdf_text(case.gateway.uploads[0][1])
    assert "confirm this drawing reference" in text and FA_REFERENCE in text


def test_review_prefix_reference_without_unique_crm_drawing_still_blocks(case):
    add_baseline(without_requested_reference(case), FA_REFERENCE, issuer=None)
    assert run(case) == ["failed"]
    assert current(case).review_json["issues"]["Drawing Reference"] == "Requested drawing/roof is missing or ambiguous"
    assert not case.gateway.uploads


def test_review_prefix_reference_without_linked_project_publishes_for_review(case):
    case.gateway.holding["column_values"][0] = relation("board_relation_mkpbm5np", [])
    requested_review_prefix_reference(case)
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["raw"]["Drawing Reference"] == FA_REFERENCE and source["raw"]["Revision"] == "26.01 - A"
    assert source["issues"]["Drawing Reference"] == REVIEW_PREFIX_REFERENCE
