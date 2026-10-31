"""Publication regressions from the allowlist audit, using synthetic documents."""

from copy import deepcopy
import io
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.legacy_enquiry.source_documents import extract_source_records, html_text
from backend.app.services.legacy_enquiry.email_extraction import extract_text_from_email
from backend.app.services.performance_specification_evidence import (
    PAGE_SIZE_CONFLICT, PAGE_SIZE_UNCERTAIN, VALUE_CONFLICT, resolve_email_evidence, resolve_request_notes,
)
from backend.app.services.performance_specification_sources import resolve_specification_sources
from backend.app.services.specification_recovery import extraction_payload
from backend.app.services.specification_semantics import tp_reference_revision
from backend.tests.test_specification_evidence import candidate, extraction, source
from backend.tests.test_performance_specification import case, current, pdf_text, run, column
from backend.app.services.performance_specification_contract import FIELD_NAMES


def parameters(**values):
    return DesignParameterExtraction(**{
        **{name: None for name in DesignParameterExtraction.model_fields if not name.startswith("specification_")},
        "specification_assessed_fields": list(FIELD_NAMES), **values,
    })


def extract_twice(initial, recovered, manifest):
    replies, calls = iter([initial.model_dump(), recovered.model_dump()]), []
    def generate(model, prompt, config):
        calls.append(prompt)
        return SimpleNamespace(parsed=next(replies))
    client = LegacyGeminiClient("offline", generate_content=generate)
    result = client.extract_design_parameters_with_manifest("Synthetic source context", manifest)
    assert len(calls) == 2 and "Verifier diagnostics" in calls[1]
    return result


def test_scale_colon_spacing_and_escaped_literal_asterisks():
    text = r"SCALE @ A1: 1 : 100. Membrane: Polyester reinforced \*\*\*PVC/ FPO\*\*\* single ply"
    data = extraction([source("asset:1:attachment:0", text)], [
        candidate("Scale", "1:100", "SCALE @ A1: 1 : 100"),
        candidate("Membrane", "Polyester reinforced ***PVC/ FPO*** single ply",
                  "Membrane: Polyester reinforced ***PVC/ FPO*** single ply"),
    ])
    raw, _, issues = resolve_email_evidence(data)
    assert raw["Scale"] == "1:100" and not issues


@pytest.mark.parametrize("value", ["1:10", "1:1000", "1/100"])
def test_scale_normalization_never_changes_the_ratio_or_separator(value):
    data = extraction([source("asset:1:attachment:0", "Scale: 1 : 100")], [candidate("Scale", value, "Scale: 1 : 100")])
    assert "Scale" in resolve_email_evidence(data)[2]


def test_literal_asterisks_cannot_be_erased_from_a_template():
    text = "Membrane: ***PVC/ FPO*** single ply"
    data = extraction([source("asset:1:attachment:0", text)], [candidate("Membrane", "PVC/ FPO single ply", text)])
    assert "Membrane" in resolve_email_evidence(data)[2]


@pytest.mark.parametrize("reference,confirmed", [
    ("AX 16670_26.12 - B", False), ("TP16670_26.12 - B", False),
    ("16670_26.12 - B", True), ("D.S - 16670_26.12 - B", True),
])
def test_accepted_reference_suffix_is_preserved(reference, confirmed):
    assert tp_reference_revision(reference, confirmed_crm=confirmed) == "26.12 - B"


def test_ax_title_block_components_and_suffix_are_verified_without_inventing_issuer():
    text = "AXTER\nDWG No: AX 16670_26.12\nREV: B\nDate: October 2026"
    evidence = candidate("Drawing Reference", "AX 16670_26.12 - B", text, issuer="AXTER",
                         reference_parts=["AX 16670_26.12", "B"])
    data = extraction([source("asset:1:attachment:0", text)], [evidence])
    raw, _, issues = resolve_email_evidence(data)
    assert raw["Drawing Reference"] == "AX 16670_26.12 - B" and not issues
    data["specificationEvidence"][0]["issuer"] = "TaperedPlus"
    assert "Drawing Reference" in resolve_email_evidence(data)[2]


@pytest.mark.parametrize("parts,value", [
    (["AX 16670_26.12", "C"], "AX 16670_26.12 - C"),
    (["AX 16670_26.12", "B"], "AX 16670_26.12 - C"),
    (["AX 16670_26.12", "B"], "TP16670_26.12 - B"),
])
def test_composite_reference_rejects_invented_letter_or_prefix(parts, value):
    text = "AXTER DWG No: AX 16670_26.12 REV: B"
    data = extraction([source("asset:1:attachment:0", text)], [
        candidate("Drawing Reference", value, text, issuer="AXTER", reference_parts=parts)])
    assert "Drawing Reference" in resolve_email_evidence(data)[2]


def test_reference_suffix_inside_full_identifier_is_valid_revision_evidence():
    text = "TaperedPlus reference D.S - 00121_26.01 - D"
    data = extraction([source("asset:1:attachment:0", text)], [candidate("Revision", "26.01 - D", text)])
    assert resolve_email_evidence(data)[0]["Revision"] == "26.01 - D"


@pytest.mark.parametrize("field,short,long", [
    ("VCL", "AVCL", "2/3mm AVCL"), ("Tapered Insulation", "HYTHERM ADH", "Tapered HYTHERM ADH"),
])
def test_duplicate_descriptions_require_same_literal_passage_and_scope(field, short, long):
    text = f"{field}: {long}"
    data = extraction([source("asset:1:attachment:0", text)], [candidate(field, short, text), candidate(field, long, text)])
    assert resolve_email_evidence(data)[0][field] == long and not resolve_email_evidence(data)[2]
    data["specificationEvidence"][0]["scope"] = "Roof 2"
    assert resolve_email_evidence(data)[2][field] == VALUE_CONFLICT


@pytest.mark.parametrize("role", ["achieved", "baseline", "withdrawn", "lookup"])
def test_nonrequested_observations_do_not_block_valid_current_request(role):
    text = "Target: 0.13"
    data = extraction([source("asset:1:attachment:0", text)], [
        candidate("Target U Value", "0.13", text),
        candidate("Target U Value", "0.08", "Unreadable historical figure", role=role),
    ])
    raw, _, issues = resolve_email_evidence(data)
    assert raw["Target U Value"] == "0.13" and not issues


def test_recovery_repairs_membrane_list_punctuation_with_literal_text_and_audit():
    text = "Membrane:\nParaflex NT3\nParaflex Ard/S"
    initial = parameters(membrane="Paraflex NT3, Paraflex Ard/S", specification_evidence=[
        candidate("Membrane", "Paraflex NT3, Paraflex Ard/S", text)])
    recovered = parameters(membrane="Paraflex NT3\nParaflex Ard/S", specification_evidence=[
        candidate("Membrane", "Paraflex NT3\nParaflex Ard/S", text)])
    manifest = [source("asset:1:attachment:0", text)]
    result = extract_twice(initial, recovered, manifest)
    assert result._evidence_repairs[0]["before"]["value"] == initial.membrane
    assert resolve_email_evidence(extraction_payload(result, manifest))[0]["Membrane"] == recovered.membrane


@pytest.mark.parametrize("ambiguous,cross_email", [(False, False), (True, False), (False, True)])
def test_wrong_attachment_id_repair_requires_unique_verified_passage_in_same_email(ambiguous, cross_email):
    old_id, new_id = "asset:1:attachment:18", "asset:2:attachment:17" if cross_email else "asset:1:attachment:17"
    text = "Block LP & AD: achieved 0.08 (Target 0.13)"
    initial = parameters(specification_evidence=[candidate("Target U Value", "0.13", text, source_id=old_id)])
    recovered = parameters(specification_evidence=[candidate("Target U Value", "0.13", text, source_id=new_id)])
    manifest = [source(old_id, "Unrelated signature"), source(new_id, text)]
    if ambiguous:
        manifest.append(source("asset:1:attachment:19", text))
    if ambiguous or cross_email:
        with pytest.raises(ValueError, match="recovery unresolved"):
            extract_twice(initial, recovered, manifest)
    else:
        result = extract_twice(initial, recovered, manifest)
        assert result.specification_evidence[0].source_id == new_id
        assert result._evidence_repairs[0]["contentSha256"] == "abc"


def test_unsupported_scalar_is_cleared_only_after_verified_achieved_classification():
    text = "U-value 0.11 EFFECTIVE"
    initial = parameters(target_u_value="0.11")
    recovered = parameters(specification_evidence=[candidate("Target U Value", "0.11", text, role="achieved")])
    result = extract_twice(initial, recovered, [source("asset:1:attachment:0", text)])
    assert result.target_u_value is None and result._evidence_repairs[0]["after"] is None
    with pytest.raises(ValueError, match="supporting evidence|recovery unresolved"):
        extract_twice(parameters(target_u_value="0.11"), parameters(), [source("asset:1:attachment:0", text)])


def test_new_valid_fact_cannot_erase_different_unverified_fact_during_recovery():
    text = "Membrane: felt"
    initial = parameters(specification_evidence=[candidate("Membrane", "single ply", "Roof: membrane")])
    recovered = parameters(specification_evidence=[candidate("Membrane", "felt", text)])
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(initial, recovered, [source("asset:1:attachment:0", text)])


def test_invalid_candidate_still_fails_when_another_pair_creates_publishable_conflict():
    text = "Roof 1: Concrete\nRoof 2: Timber"
    candidates = [candidate("Decking", "Concrete", "Roof 1: Concrete", scope="Roof 1"),
                  candidate("Decking", "Timber", "Roof 2: Timber", scope="Roof 2"),
                  candidate("Decking", "Steel", "Invented", scope="Roof 3")]
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(parameters(specification_evidence=candidates), parameters(specification_evidence=candidates),
                      [source("asset:1:attachment:0", text)])


def test_pdf_dimensions_flag_misread_size_without_replacing_literal_title_block():
    with PdfWriter() as writer:
        writer.add_blank_page(width=841 * 72 / 25.4, height=594 * 72 / 25.4)
        output = io.BytesIO()
        writer.write(output)
    manifest = extract_source_records(output.getvalue(), "drawing.pdf", SimpleNamespace(process_pdf=lambda *args: "ISO A3"),
                                      source_id="asset:1:attachment:0")
    assert manifest[0]["pdfPageSizes"] == ["A1"]
    raw, _, issues = resolve_email_evidence(extraction(manifest, [candidate("Page Size", "ISO A3", "ISO A3")]))
    assert raw["Page Size"] == "ISO A3" and issues["Page Size"] == PAGE_SIZE_CONFLICT
    manifest[0]["text"] = "ISO AB"
    assert resolve_email_evidence(extraction(manifest, [candidate("Page Size", "ISO AB", "ISO AB")]))[2]["Page Size"] == PAGE_SIZE_UNCERTAIN


def test_inline_content_id_links_surrounding_scope_to_exact_attachment():
    body = html_text('<p>Block LP &amp; AD</p><img src="cid:scope-image"><p>Other block</p>')
    assert body.index("Block LP & AD") < body.index("cid:scope-image") < body.index("Other block")
    manifest = []
    text = extract_text_from_email(body, [{"filename": "image018.txt", "content": b"Target 0.13", "content_id": "<scope-image>"}],
                                   extractor=SimpleNamespace(), manifest=manifest, source_prefix="asset:1")
    assert manifest[1]["contentId"] == "scope-image"
    assert "SOURCE ID: asset:1:attachment:0" in text and "CONTENT-ID: scope-image" in text


def test_mixed_crm_materials_publish_null_cad_value_with_readable_alternatives(case):
    case.gateway.project["subitems"][0]["column_values"][1] = {
        **column("mirror875__1", "Product Type", None, "dropdown"),
        "values": [{"id": "1", "label": "Tissue Faced PIR"}, {"id": "2", "label": "VIPs"}],
    }
    assert run(case) == ["published"]
    record = current(case)
    assert record.source_json["raw"]["Tapered Insulation"] is None
    assert record.review_json["translation"]["values"]["Tapered Insulation"] is None
    text = pdf_text(case.gateway.uploads[-1][1])
    assert "Tissue Faced PIR" in text and "VIPs" in text and "Tapered Insulation: Review required" in text


def test_mirrored_mixed_material_does_not_hide_an_unreadable_second_source(case):
    case.gateway.project["subitems"][0]["column_values"][1] = {
        **column("mirror875__1", "Product Type", None, "mirror"),
        "mirrored_items": [
            {"linked_item": {"id": "a", "board": {"id": "b"}}, "mirrored_value": {
                "values": [{"label": "PIR"}, {"label": "VIPs"}]}},
            {"linked_item": {"id": "c", "board": {"id": "b"}}, "mirrored_value": {}},
        ],
    }
    assert run(case) == ["failed"] and not case.gateway.uploads


def test_ax_crm_reference_is_selected_and_keeps_its_full_revision(case):
    case.gateway.project["subitems"][0]["name"] = "AX 16670_26.12 - B"
    data = deepcopy(case.item.extracted_parameters_json)
    data["performanceParameters"]["Drawing Reference"] = "AX 16670_26.12 - B"
    for evidence in data["specificationEvidence"]:
        if evidence["field"] == "Drawing Reference":
            evidence.update(value="AX 16670_26.12 - B", snippet="AXTER AX 16670_26.12 - B", issuer="AXTER")
    data["sourceManifest"][0]["text"] += "\nAXTER AX 16670_26.12 - B"
    case.item.extracted_parameters_json = data
    resolved = resolve_specification_sources(case.gateway, case.item)
    assert resolved["raw"]["Drawing Reference"] == "AX 16670_26.12 - B"
    assert resolved["raw"]["Revision"] == "26.12 - B"


def test_verified_constraints_are_published_and_bad_notes_preserve_last_pdf(case):
    data = deepcopy(case.item.extracted_parameters_json)
    snippet = "VIPs everywhere except the three outlets; tapered gutter maximum 70mm. Exclude the pink roof."
    data["sourceManifest"][0]["text"] += "\n" + snippet
    data["specificationRequestNotes"] = [{"source_id": "email:body", "snippet": snippet, "category": "constraint", "scope": None}]
    case.item.extracted_parameters_json = data
    assert run(case) == ["published"]
    revision = current(case).current_revision
    text = pdf_text(case.gateway.uploads[-1][1])
    assert snippet in text and "Request constraints and context" in text
    assert text.index("Decking:") < text.index("Request constraints and context")
    assert current(case).review_json["status"] == "published_with_review"
    bad = deepcopy(data)
    bad["specificationRequestNotes"][0]["snippet"] = "Invented instruction"
    case.item.extracted_parameters_json = bad
    case.db.commit()
    assert run(case) == ["failed"] and current(case).current_revision == revision
    assert len(case.gateway.uploads) == 1


def test_request_note_scope_must_be_in_the_same_source():
    data = extraction([source("asset:1:attachment:0", "Maximum 70mm")], [])
    data["specificationRequestNotes"] = [{"source_id": "asset:1:attachment:0", "snippet": "Maximum 70mm",
                                        "category": "constraint", "scope": "Invented roof"}]
    with pytest.raises(ValueError, match="unverified roof scope"):
        resolve_request_notes(data)


@pytest.mark.parametrize("variant", ["old_revision", "different_model"])
def test_matching_stored_identities_cannot_publish_an_obsolete_configured_pipeline(case, variant):
    assert run(case) == ["published"]
    revision = current(case).current_revision
    data = deepcopy(case.item.extracted_parameters_json)
    pipeline = data["pipelineVersion"]
    obsolete = pipeline.replace("output-v15", "output-v14") if variant == "old_revision" else "other-model:" + pipeline
    data["pipelineVersion"] = obsolete
    case.item.extracted_parameters_json = data
    case.item.latest_analyzed_pipeline_version = case.item.latest_desired_pipeline_version = obsolete
    case.db.commit()
    assert run(case) == ["failed"] and current(case).current_revision == revision
    assert "current configured pipeline" in current(case).last_error and len(case.gateway.uploads) == 1


def test_ambiguous_paper_size_publishes_review_instead_of_guessing_a3(case):
    data = deepcopy(case.item.extracted_parameters_json)
    data["performanceParameters"]["Page Size"] = "ISO AB"
    data["sourceManifest"][0]["text"] += "\nPaper size: ISO AB"
    for evidence in data["specificationEvidence"]:
        if evidence["field"] == "Page Size":
            evidence.update(value="ISO AB", snippet="Paper size: ISO AB")
    case.item.extracted_parameters_json = data
    assert run(case) == ["published"]
    record = current(case)
    assert record.review_json["translation"]["values"]["Page Size"] is None
    text = pdf_text(case.gateway.uploads[-1][1])
    assert "Page Size: Review required" in text and "ISO AB" in text and "ISO A3" not in text


def test_issuer_repair_requires_literal_branding_and_preserves_audit():
    text = "AXTER DWG No: AX 16670_26.12 REV: B"
    old = candidate("Drawing Reference", "AX 16670_26.12 - B", "AX 16670_26.12 - B", issuer="TaperedPlus")
    new = candidate("Drawing Reference", "AX 16670_26.12 - B", text, issuer="AXTER",
                    reference_parts=["AX 16670_26.12", "B"])
    result = extract_twice(parameters(specification_evidence=[old]), parameters(specification_evidence=[new]),
                           [source("asset:1:attachment:0", text)])
    assert result._evidence_repairs[0]["before"]["issuer"] == "TaperedPlus"
    assert result.specification_evidence[0].issuer == "AXTER"


@pytest.mark.parametrize("old_value,new_value", [("-0.13", "0.13"), ("0.13", "0.130"), ("0,13", "0.13")])
def test_citation_repair_cannot_change_signed_numbers_or_decimal_text(old_value, new_value):
    text = f"Target: {new_value}"
    initial = parameters(specification_evidence=[candidate("Target U Value", old_value, "Unsupported excerpt")])
    recovery = parameters(specification_evidence=[candidate("Target U Value", new_value, text)])
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(initial, recovery, [source("asset:1:attachment:0", text)])
