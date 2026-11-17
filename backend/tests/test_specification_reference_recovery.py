"""Offline regressions for correcting external drawing identity classifications."""

import pytest

from backend.app.services.performance_specification_evidence import (
    VALUE_CONFLICT,
    resolve_baseline_drawing_identity,
    resolve_email_evidence,
)
from backend.app.services.specification_recovery import (
    _external_reference_context_proof, extraction_payload, merge_recovery,
)
from backend.app.services.specification_semantics import SpecificationEvidence
from backend.tests.test_specification_evidence import candidate as source_candidate, source
from backend.tests.test_specification_publication_hardening import extract_twice, parameters


EXTERNAL_REFERENCE = "082-ARC-ZZ-RF-DR-A-08007-P01"
EXTERNAL_ISSUER = "Example Architects Ltd"
EXTERNAL_TITLE_BLOCK = f"Project Origin Level Type Number Rev\n{EXTERNAL_REFERENCE}"
EXTERNAL_TEXT = (f"This drawing and design is the sole property of {EXTERNAL_ISSUER}.\n"
                 f"{EXTERNAL_TITLE_BLOCK}\nScale: As indicated\nPlan scale: 1:100")
EXTERNAL_ID = "asset:1:attachment:0"
BASELINE_REFERENCE = "AX18603_26.02 - C"
BASELINE_TEXT = f"Please find attached quotation and drawing for {BASELINE_REFERENCE} (Roof 2)."
BASELINE_ID = "asset:1:attachment:1"


def candidate(*args, **changes):
    return SpecificationEvidence.model_validate(source_candidate(*args, **changes)).model_dump()


def external_candidate(**changes):
    return candidate(
        "Drawing Reference", EXTERNAL_REFERENCE, EXTERNAL_REFERENCE,
        issuer=EXTERNAL_ISSUER, **changes,
    )


def external_recovery(**changes):
    return {**external_candidate(), "role": "baseline", "snippet": EXTERNAL_TITLE_BLOCK, **changes}


def external_manifest():
    return [source(EXTERNAL_ID, EXTERNAL_TEXT)]


@pytest.mark.parametrize("snippet", [EXTERNAL_TITLE_BLOCK, EXTERNAL_TEXT])
@pytest.mark.parametrize("reference_parts", [[], ["082-ARC-ZZ-RF-DR-A-08007", "P01"]])
def test_external_reference_reclassification_replaces_rejected_original_and_is_audited(snippet, reference_parts):
    original = external_candidate()
    corrected = external_recovery(snippet=snippet, reference_parts=reference_parts)
    manifest = external_manifest()
    result = extract_twice(
        parameters(specification_evidence=[original]),
        parameters(specification_evidence=[corrected]),
        manifest,
    )
    assert [entry.model_dump() for entry in result.specification_evidence] == [corrected]
    raw, provenance, issues = resolve_email_evidence(extraction_payload(result, manifest))
    assert raw["Drawing Reference"] is None and not issues
    assert not provenance["Drawing Reference"].get("rejectedCandidates")
    repair, = result._evidence_repairs
    assert repair["before"] == original and repair["after"] == corrected
    assert repair["contentSha256"] == "abc"
    assert repair["issuerProof"] in EXTERNAL_TEXT
    assert EXTERNAL_ISSUER in repair["issuerProof"] and "property of" in repair["issuerProof"]


def test_baseline_identity_issuer_repair_and_external_context_leave_scale_reviewable():
    baseline = candidate(
        "Drawing Reference", BASELINE_REFERENCE, BASELINE_TEXT,
        source_id=BASELINE_ID, issuer="TaperedPlus", role="baseline", scope="Roof 2",
    )
    revision = candidate(
        "Revision", "26.02 - C", BASELINE_TEXT,
        source_id=BASELINE_ID, role="baseline", scope="Roof 2",
    )
    scales = [
        candidate("Scale", "1:100", "Plan scale: 1:100"),
        candidate("Scale", "As indicated", "Scale: As indicated"),
    ]
    original = external_candidate()
    corrected = external_recovery()
    corrected_baseline = {**baseline, "issuer": None}
    manifest = external_manifest() + [source(BASELINE_ID, BASELINE_TEXT)]
    result = extract_twice(
        parameters(drawing_reference=BASELINE_REFERENCE, revision="26.02 - C", scale="1:100",
                   specification_evidence=[original, baseline, revision, *scales]),
        parameters(specification_evidence=[corrected, corrected_baseline, revision, *scales]),
        manifest,
    )
    assert result.drawing_reference is None and result.revision is None
    references = [entry.model_dump() for entry in result.specification_evidence if entry.field == "Drawing Reference"]
    assert original not in references and baseline not in references
    assert corrected in references and corrected_baseline in references
    payload = extraction_payload(result, manifest)
    raw, _, issues = resolve_email_evidence(payload)
    assert raw["Drawing Reference"] is None
    assert issues == {"Scale": VALUE_CONFLICT}
    baselines, reason = resolve_baseline_drawing_identity(payload)
    assert reason is None
    assert [entry["value"] for entry in baselines] == [BASELINE_REFERENCE]
    assert baselines[0]["issuer"] is None
    assert any(repair.get("before") == baseline and repair.get("after", {}).get("issuer") is None
               for repair in result._evidence_repairs)


def test_explicit_drawing_authorship_also_establishes_external_context():
    proof = f"Drawing issued by {EXTERNAL_ISSUER}"
    text = f"{proof}\n{EXTERNAL_TITLE_BLOCK}"
    corrected = external_recovery()
    result = extract_twice(parameters(specification_evidence=[external_candidate()]),
                          parameters(specification_evidence=[corrected]),
                          [source(EXTERNAL_ID, text)])
    assert [entry.model_dump() for entry in result.specification_evidence] == [corrected]
    assert result._evidence_repairs[0]["issuerProof"] == proof


def test_unquoted_baseline_issuer_is_repaired_without_promoting_the_baseline_scalar():
    original = candidate("Drawing Reference", BASELINE_REFERENCE, BASELINE_TEXT,
                         source_id=BASELINE_ID, issuer="TaperedPlus", role="baseline", scope="Roof 2")
    corrected = {**original, "issuer": None}
    manifest = [source(BASELINE_ID, BASELINE_TEXT)]
    result = extract_twice(
        parameters(drawing_reference=BASELINE_REFERENCE, specification_evidence=[original]),
        parameters(specification_evidence=[corrected]), manifest,
    )
    references = [entry.model_dump() for entry in result.specification_evidence]
    assert original not in references and corrected in references
    assert result.drawing_reference is None
    baselines, reason = resolve_baseline_drawing_identity(extraction_payload(result, manifest))
    assert reason is None and [entry["value"] for entry in baselines] == [BASELINE_REFERENCE]
    assert any(repair.get("before") == original and repair.get("after", {}).get("issuer") is None
               for repair in result._evidence_repairs)


@pytest.mark.parametrize("reference", [
    "TP01234_26.01 - A", "AX01234_26.01 - A", "01234_26.01 - A", "D.S - 01234_26.01 - A",
])
def test_existing_reference_families_cannot_be_demoted_through_external_context_repair(reference):
    text = f"This drawing is the property of {EXTERNAL_ISSUER}.\nDrawing: {reference}"
    original = candidate("Drawing Reference", reference, text, issuer=EXTERNAL_ISSUER)
    corrected = {**original, "role": "baseline"}
    manifest = [source(EXTERNAL_ID, text)]
    # Exercise the structural guard itself: valid requested evidence would skip
    # the failed-claim repair branch in merge_recovery.
    assert _external_reference_context_proof(
        SpecificationEvidence.model_validate(original), SpecificationEvidence.model_validate(corrected),
        {EXTERNAL_ID: manifest[0]},
    ) is None
    result = extract_twice(
        parameters(drawing_reference=reference, specification_evidence=[original]),
        parameters(specification_evidence=[corrected]), manifest,
    )
    assert original in [entry.model_dump() for entry in result.specification_evidence]
    assert result.drawing_reference == reference
    assert resolve_email_evidence(extraction_payload(result, manifest))[0]["Drawing Reference"] == reference
    assert not any(repair.get("before") == original and repair.get("after", {}).get("role") == "baseline"
                   for repair in result._evidence_repairs)


@pytest.mark.parametrize("change", [
    {"value": "082-ARC-ZZ-RF-DR-A-08007-P02"},
    {"source_id": "asset:1:attachment:2"},
    {"scope": "Roof 2"},
    {"request_role": "forwarded_request"},
    {"message_date": "13 October 2026 11:40"},
    {"issuer": "Another Architects Ltd"},
    {"snippet": "An invented title block"},
])
def test_external_context_repair_cannot_change_claim_or_request_context(change):
    manifest = external_manifest() + [source("asset:1:attachment:2", EXTERNAL_TEXT)]
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(
            parameters(specification_evidence=[external_candidate()]),
            parameters(specification_evidence=[external_recovery(**change)]), manifest,
        )


def test_unquoted_external_issuer_does_not_authorize_reclassification():
    original = {**external_candidate(), "issuer": "Invented Architects Ltd"}
    corrected = {**external_recovery(), "issuer": "Invented Architects Ltd"}
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(parameters(specification_evidence=[original]),
                      parameters(specification_evidence=[corrected]), external_manifest())


def test_client_name_in_source_does_not_establish_external_drawing_ownership():
    text = f"Client: {EXTERNAL_ISSUER}\n{EXTERNAL_TITLE_BLOCK}"
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(
            parameters(specification_evidence=[external_candidate()]),
            parameters(specification_evidence=[external_recovery()]),
            [source(EXTERNAL_ID, text)],
        )


@pytest.mark.parametrize("statement", [
    "This drawing is not the property of",
    "This plan was not issued by",
    "This drawing isn't the property of",
    "This drawing isn\u2019t the property of",
    "This drawing wasn't prepared by",
    "No drawing is the property of",
    "This is not a drawing issued by",
])
def test_negated_ownership_does_not_authorize_external_context_reclassification(statement):
    text = f"{statement} {EXTERNAL_ISSUER}.\n{EXTERNAL_TITLE_BLOCK}"
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(parameters(specification_evidence=[external_candidate()]),
                      parameters(specification_evidence=[external_recovery()]),
                      [source(EXTERNAL_ID, text)])


def test_external_reference_parts_cannot_assemble_a_missing_full_literal():
    split_block = "Drawing number: 082-ARC-ZZ-RF-DR-A-08007\nRevision: P01"
    text = EXTERNAL_TEXT + "\n" + split_block
    corrected = external_recovery(snippet=split_block,
                                  reference_parts=["082-ARC-ZZ-RF-DR-A-08007", "P01"])
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(parameters(specification_evidence=[external_candidate()]),
                      parameters(specification_evidence=[corrected]),
                      [source(EXTERNAL_ID, text)])


def test_distant_ownership_statement_cannot_be_borrowed_from_another_sheet():
    text = (f"This drawing is the property of {EXTERNAL_ISSUER}.\n"
            + "Unrelated drawing content.\n" * 200 + EXTERNAL_TITLE_BLOCK)
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(parameters(specification_evidence=[external_candidate()]),
                      parameters(specification_evidence=[external_recovery()]),
                      [source(EXTERNAL_ID, text)])


def test_unverified_original_excerpt_cannot_be_hidden_by_context_reclassification():
    original = {**external_candidate(), "snippet": "An invented original passage"}
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(parameters(specification_evidence=[original]),
                      parameters(specification_evidence=[external_recovery()]), external_manifest())


def test_ambiguous_external_context_corrections_preserve_original_failure():
    corrected = external_recovery()
    competing = external_recovery(snippet=EXTERNAL_TEXT)
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(
            parameters(specification_evidence=[external_candidate()]),
            parameters(specification_evidence=[corrected, competing]), external_manifest(),
        )


def test_identical_external_context_corrections_are_one_unambiguous_repair():
    corrected = external_recovery()
    result = extract_twice(parameters(specification_evidence=[external_candidate()]),
                          parameters(specification_evidence=[corrected, corrected]), external_manifest())
    assert [entry.model_dump() for entry in result.specification_evidence] == [corrected]


def test_other_requested_fields_cannot_be_demoted_by_external_reference_repair():
    text = "Membrane: bituminous"
    original = candidate("Membrane", "bituminous", text)
    corrected = {**original, "role": "baseline"}
    manifest = [source(EXTERNAL_ID, text)]
    result = extract_twice(parameters(membrane="bituminous", specification_evidence=[original]),
                          parameters(specification_evidence=[corrected]), manifest)
    assert original in [entry.model_dump() for entry in result.specification_evidence]
    assert result.membrane == "bituminous"


def test_unrelated_unverified_request_does_not_hold_baseline_scalar_or_disappear():
    baseline = candidate("Drawing Reference", BASELINE_REFERENCE, BASELINE_TEXT,
                         source_id=BASELINE_ID, role="baseline")
    rejected = {**external_candidate(), "snippet": "An invented request passage"}
    manifest = external_manifest() + [source(BASELINE_ID, BASELINE_TEXT)]
    initial = parameters(drawing_reference=BASELINE_REFERENCE, specification_evidence=[baseline, rejected])
    recovery = parameters(specification_evidence=[baseline])
    merge_recovery(initial, recovery, manifest)
    assert initial.drawing_reference is None
    assert rejected in [entry.model_dump() for entry in initial.specification_evidence]
    _, provenance, issues = resolve_email_evidence(extraction_payload(initial, manifest))
    assert issues["Drawing Reference"] == "Supporting source/excerpt could not be verified"
    assert provenance["Drawing Reference"]["rejectedCandidates"][0]["status"] == "unverified"


def test_valid_current_request_remains_alongside_repaired_baseline_and_external_context():
    current_reference = "TP01234_26.03 - A"
    current_text = f"Please revise drawing {current_reference}."
    current = candidate("Drawing Reference", current_reference, current_text,
                        source_id="asset:1:attachment:2")
    baseline = candidate("Drawing Reference", BASELINE_REFERENCE, BASELINE_TEXT,
                         source_id=BASELINE_ID, role="baseline")
    manifest = external_manifest() + [source(BASELINE_ID, BASELINE_TEXT),
                                      source("asset:1:attachment:2", current_text)]
    result = extract_twice(
        parameters(drawing_reference=current_reference,
                   specification_evidence=[external_candidate(), baseline, current]),
        parameters(specification_evidence=[external_recovery(), baseline, current]), manifest,
    )
    assert result.drawing_reference == current_reference
    raw, _, issues = resolve_email_evidence(extraction_payload(result, manifest))
    assert raw["Drawing Reference"] == current_reference and not issues
