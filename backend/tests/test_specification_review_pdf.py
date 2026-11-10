"""Reviewer display is authoritative while CAD choices stay independently strict."""

from copy import deepcopy
import io
import json
import re
from types import SimpleNamespace

import pytest

from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.performance_specification_contract import (
    CADTranslation, FIELD_NAMES, SPECIFICATION_VERSION, pdf_values,
    prepare_specification_publication, reviewer_cad_mapping, specification_translation_inputs,
)
from backend.app.services.performance_specification_pdf import render_performance_specification_pdf
from backend.app.services.specification_review_artifacts import build_review_artifact_metadata
from backend.app.services.specification_review_contract import (
    AutomaticProposal, ReviewDecisionState, ReviewSourceSnapshot,
)


IDENTITY = {
    "source_fingerprint": "email-and-crm-v1", "extraction_identity": "extraction-v1",
    "source_policy_version": "source-v1", "reference_parser_version": "parser-v1",
}
AUDIT = {
    "actor": {"app_user_id": "reviewer-1", "monday_user_id": "100", "monday_account_id": "200"},
    "saved_at": "2026-11-09T10:00:00+00:00", "reviewed_source": IDENTITY,
}
RAW = {
    "Scale": "1:100", "Page Size": "ISO A1", "Drawing Reference": None,
    "Drawing Title": "Warehouse roof", "Bauder Contract Number": "00123",
    "Bauder Roof Num": "007", "Incoming Information": "", "Revision": None,
    "Date Received": "9 Nov 2026", "Company": "Email company", "Contact": "Alex Example",
    "Surveyor": "Jo Example", "Target U Value": "0.18", "Target Min U Value": "0.15",
    "Membrane": "Single Ply", "Tapered Insulation": "Tissue Faced PIR",
    "VCL": "Felt AVCL", "Decking": "Concrete",
}


def source_with(decisions=None):
    fields = {}
    sources = {}
    for label in FIELD_NAMES:
        value = RAW[label]
        kind = "default_blank" if label == "Incoming Information" else "unassigned" if value is None else "email_context"
        evidence = [{"source_id": "email:body", "excerpt": f"{label}: {value}", "location": "client request"}] if kind == "email_context" else []
        proposal = AutomaticProposal(
            proposal_id=label, display_value=value, source=kind, rationale="Current client request",
            email_evidence=evidence, cad_mapping={"status": "unmapped" if kind == "email_context" else "not_applicable"},
        )
        fields[label] = {"proposal": proposal}
        sources[label] = {"selected": {"value": value, "snippet": f"{label}: {value}", "observedAt": "ignored"}}
    snapshot = ReviewSourceSnapshot(
        identity=IDENTITY, intent={"recommended_type": "uncertain", "explanation": "Fixture"},
        request_type="new_enquiry", fields=fields,
    )
    state = ReviewDecisionState(review_version=1, field_decisions={
        label: {"decision": decision, "audit": AUDIT} for label, decision in (decisions or {}).items()
    })
    metadata = build_review_artifact_metadata(state, snapshot)
    return {
        "raw": {label: metadata["field_provenance"][label]["display_value"] for label in FIELD_NAMES},
        "sources": sources, "issues": {}, "sourceManifest": [],
        "reviewSource": snapshot.model_dump(mode="json"), "review": metadata,
    }


def client():
    calls = []

    def generate(model, prompt, config):
        values = json.loads(prompt.split("\nResolved values: ", 1)[1])
        calls.append(values)
        return SimpleNamespace(parsed={"values": values, "warnings": []})

    return LegacyGeminiClient("offline", generate_content=generate), calls


def extracted_text(content):
    pypdf = pytest.importorskip("pypdf")
    return "\n".join(page.extract_text() for page in pypdf.PdfReader(io.BytesIO(content)).pages)


@pytest.mark.parametrize("label", FIELD_NAMES)
def test_every_pdf_field_preserves_literal_reviewer_text_and_separate_source(label):
    literal = "Zoë & Müller <roof>\nReviewer's exact display"
    source = source_with({label: {"operation": "specify_value", "display_value": literal}})
    source["issues"][label] = "Old conflicting recommendation"
    model, calls = client()
    publication = prepare_specification_publication(source, model)
    assert pdf_values(publication)[label] == literal
    assert publication.field_sources[label] == "Reviewer Specified"
    assert label not in publication.field_reviews
    assert label in publication.mapping_issues
    assert calls[0][label] == ("" if label == "Incoming Information" else None)
    text = extracted_text(render_performance_specification_pdf(publication, item_id="123", revision="test"))
    normalized = " ".join(text.split())
    assert f"{label}: {' '.join(literal.split())} Source: Reviewer Specified" in normalized


@pytest.mark.parametrize("label", FIELD_NAMES)
def test_intentional_blank_never_uses_fallback_or_not_provided(label):
    source = source_with({label: {"operation": "specify_blank"}})
    publication = prepare_specification_publication(source, client()[0])
    assert pdf_values(publication)[label] == ""
    assert publication.field_sources[label] == "Reviewer Specified"
    assert label not in publication.mapping_issues


def test_acceptance_preserves_snapshot_source_and_restore_uses_current_proposal():
    source = source_with()
    accepted = deepcopy(source["reviewSource"]["fields"]["Decking"]["proposal"])
    accepted["display_value"] = "Timber (18mm)"
    accepted["cad_mapping"] = reviewer_cad_mapping("Decking", "Timber (18mm)").model_dump()
    accepted_source = source_with({"Decking": {"operation": "accept_recommendation", "snapshot": accepted}})
    publication = prepare_specification_publication(accepted_source, client()[0])
    assert pdf_values(publication)["Decking"] == "Timber (18mm)"
    assert publication.values.model_dump(by_alias=True)["Decking"] == "Timber (18mm)"
    assert publication.field_sources["Decking"] == "Email Context"
    restored = prepare_specification_publication(source_with(), client()[0])
    assert pdf_values(restored)["Decking"] == "Concrete"


def test_valid_reviewer_mapping_bypasses_translation_and_retains_literal_numeric_display():
    decisions = {label: {"operation": "specify_value", "display_value": value,
                        "cad_mapping": reviewer_cad_mapping(label, value).model_dump()}
                 for label, value in RAW.items() if value}
    decisions["Target U Value"]["display_value"] = "0.180"
    source = source_with(decisions)
    model, calls = client()
    publication = prepare_specification_publication(source, model)
    assert calls == []
    assert publication.values.model_dump(by_alias=True)["Target U Value"] == "0.18"
    assert pdf_values(publication)["Target U Value"] == "0.180"
    assert publication.values.model_dump(by_alias=True)["Company"] is None
    assert "Company" in publication.mapping_issues


@pytest.mark.parametrize("label,value,status", [
    ("Decking", "Concrete", "mapped"), ("Decking", "Custom board", "unmapped"),
    ("Scale", "1:200", "unmapped"), ("Scale", "1:100", "mapped"),
    ("Target U Value", "0.185", "mapped"), ("Target U Value", "-0.18", "unmapped"),
    ("Target U Value", "0", "unmapped"), ("Date Received", "31 Feb 2026", "unmapped"),
    ("Date Received", "9 Nov 2026", "mapped"), ("Contact", "Alex Example", "unmapped"),
])
def test_reviewer_mapping_never_invents_catalogue_id_or_nearest_choice(label, value, status):
    mapping = reviewer_cad_mapping(label, value)
    assert mapping.status == status
    assert mapping.target_identity is None
    if status == "mapped":
        assert mapping.catalogue_version == SPECIFICATION_VERSION


def test_cached_translation_rebuilds_current_display_and_notes_without_model_work():
    model, calls = client()
    first = source_with({"Decking": {"operation": "specify_value", "display_value": "Custom deck"}})
    initial = prepare_specification_publication(first, model)
    second = source_with({"Decking": {"operation": "specify_blank"}})
    second["sources"]["Contact"]["selected"]["observedAt"] = "a new polling timestamp"
    second["review"]["review_version"] = 2
    assert specification_translation_inputs(first) == specification_translation_inputs(second)
    cached = CADTranslation(values=initial.values, warnings=initial.warnings)
    updated = prepare_specification_publication(second, model, cached_translation=cached)
    assert len(calls) == 1
    assert pdf_values(updated)["Decking"] == ""
    assert updated.review_version == 2


def test_source_level_issues_survive_unrelated_authoritative_edit():
    source = source_with({"Decking": {"operation": "specify_blank"}})
    source["reviewSource"]["source_issues"] = [{"source_id": "file:missing", "code": "unread", "detail": "Roof drawing could not be read"}]
    publication = prepare_specification_publication(source, client()[0])
    assert publication.source_reviews == ["Roof drawing could not be read"]


def test_mapped_automatic_recommendation_with_unresolved_source_issue_stays_for_review():
    source = source_with()
    source["review"]["field_provenance"]["Target U Value"]["cad_mapping"] = reviewer_cad_mapping(
        "Target U Value", "0.18").model_dump()
    source["issues"]["Target U Value"] = "The selected drawing's U-value could not be read"
    publication = prepare_specification_publication(source, client()[0])
    assert publication.values.model_dump(by_alias=True)["Target U Value"] is None
    assert "selected drawing's U-value" in publication.field_reviews["Target U Value"]
    assert pdf_values(publication)["Target U Value"].startswith("Review required")


def test_pdf_provenance_does_not_become_a_downstream_field_value():
    source = source_with({"Decking": {"operation": "specify_value", "display_value": "Timber (18mm)",
                                     "cad_mapping": reviewer_cad_mapping("Decking", "Timber (18mm)").model_dump()}})
    publication = prepare_specification_publication(source, client()[0])
    text = extracted_text(render_performance_specification_pdf(publication, item_id="123", revision="test"))
    # Representative CAD line extraction: only exact label-prefixed lines are
    # values. Provenance and mapping notes are their own selectable text lines.
    pattern = re.compile(r"^(" + "|".join(re.escape(label) for label in FIELD_NAMES) + r"): *(.*)$", re.MULTILINE)
    rows = pattern.findall(text)
    assert [label for label, _ in rows] == list(FIELD_NAMES)
    values = dict(rows)
    assert values["Decking"] == "Timber (18mm)"
    assert all("Source:" not in value for value in values.values())


def test_long_values_unicode_and_source_labels_remain_selectable_across_pages():
    literal = "Zoë Müller & roof <north> " * 100
    source = source_with({"Drawing Title": {"operation": "specify_value", "display_value": literal}})
    publication = prepare_specification_publication(source, client()[0])
    content = render_performance_specification_pdf(publication, item_id="123", revision="long")
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(content))
    assert len(reader.pages) >= 2
    text = " ".join(extracted_text(content).split())
    assert "Zoë Müller & roof <north>" in text
    assert text.count("Source:") == len(FIELD_NAMES)
    assert "Specification review version 1" in text
