"""Offline regressions for Render evidence failures; no source facts are invented."""

from types import SimpleNamespace

import pytest

from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.performance_specification_evidence import resolve_email_evidence, resolve_request_notes
from backend.app.services.specification_recovery import extraction_payload
from backend.tests.test_specification_evidence import candidate, extraction, source
from backend.tests.test_specification_publication_hardening import extract_twice, parameters


def note(snippet, **overrides):
    return {"source_id": "asset:1:attachment:0", "snippet": snippet,
            "scope": None, "category": "constraint", **overrides}


@pytest.mark.parametrize("explicit_parts", [False, True])
@pytest.mark.parametrize("number", ["AX 16670_26.12", "TP16670_26.12", "D.S - 16670_26.12"])
def test_revision_from_separate_title_block_cells_survives_extraction(explicit_parts, number):
    text = f"DWG No: {number}\nREV: B"
    evidence = candidate("Revision", "26.12 - B", text,
                         reference_parts=[number, "B"] if explicit_parts else [])
    initial = parameters(revision="26.12 - B", specification_evidence=[evidence])
    manifest = [source("asset:1:attachment:0", text)]
    result = extract_twice(initial, initial, manifest)
    raw, _, issues = resolve_email_evidence(extraction_payload(result, manifest))
    assert raw["Revision"] == "26.12 - B" and not issues


@pytest.mark.parametrize("snippet,value", [
    ("DWG No: AX 16670_26.12 REV: B", "26.12 - C"),
    ("DWG No: AX 16670_26.12 REV: B", "26.13 - B"),
    ("DWG No: AX 16670_26.12 REV: B REV: C", "26.12 - B"),
    ("DWG No: AX 16670_26.12 Other: AX 16671_26.13 REV: B", "26.12 - B"),
    ("DWG No: AX 16670_26.12 Date: B", "26.12 - B"),
    ("DWG No: AX 16670_26.12.3 REV: B", "26.12 - B"),
    ("DWG No: AX 16670_26.12.3", "26.12"),
])
def test_composite_revision_rejects_changed_or_ambiguous_parts(snippet, value):
    data = extraction([source("asset:1:attachment:0", snippet)], [candidate("Revision", value, snippet)])
    assert "Revision" in resolve_email_evidence(data)[2]


def test_composite_revision_cannot_splice_two_source_passages():
    text = "DWG No: AX 16670_26.12\nOther roof details\nREV: B"
    evidence = candidate("Revision", "26.12 - B", "DWG No: AX 16670_26.12 REV: B")
    assert "Revision" in resolve_email_evidence(extraction([source("asset:1:attachment:0", text)], [evidence]))[2]


def test_title_block_number_can_end_with_sentence_punctuation():
    text = "Drawing: AX 16670_26.12. Revision: B."
    data = extraction([source("asset:1:attachment:0", text)], [candidate("Revision", "26.12 - B", text)])
    assert resolve_email_evidence(data)[0]["Revision"] == "26.12 - B"


def test_corrected_request_note_replaces_only_invalid_version_and_is_audited():
    text = "Maximum height 150 mm. Exclude the pink roof."
    initial = parameters(specification_request_notes=[note("Maximum height: 150 mm."), note("Exclude the pink roof.")])
    recovery = parameters(specification_request_notes=[note("Maximum height 150 mm.")])
    manifest = [source("asset:1:attachment:0", text)]
    result = extract_twice(initial, recovery, manifest)
    notes = resolve_request_notes(extraction_payload(result, manifest))
    assert {n["snippet"] for n in notes} == {"Maximum height 150 mm.", "Exclude the pink roof."}
    repair, = result._evidence_repairs
    assert repair["before"]["snippet"] == "Maximum height: 150 mm."
    assert repair["after"]["snippet"] == "Maximum height 150 mm."
    assert repair["contentSha256"] == "abc"


@pytest.mark.parametrize("case", ["unique", "ambiguous", "cross_email"])
def test_request_note_wrong_attachment_repair_requires_unique_same_email_source(case):
    text = "Maximum height 150 mm."
    old_id = "asset:1:attachment:0"
    new_id = "asset:2:attachment:1" if case == "cross_email" else "asset:1:attachment:1"
    manifest = [source(old_id, "Signature"), source(new_id, text)]
    if case == "ambiguous":
        manifest.append(source("asset:1:attachment:2", text))
    initial = parameters(specification_request_notes=[note(text, source_id=old_id)])
    recovery = parameters(specification_request_notes=[note(text, source_id=new_id)])
    if case == "unique":
        result = extract_twice(initial, recovery, manifest)
        assert result.specification_request_notes[0].source_id == new_id
        assert result._evidence_repairs[0]["before"]["source_id"] == old_id
    else:
        with pytest.raises(ValueError, match="unverified source/excerpt"):
            extract_twice(initial, recovery, manifest)


@pytest.mark.parametrize("old,new,overrides", [
    ("Maximum height: 150 mm.", "Maximum height 160 mm.", {}),
    ("Do not include the pink roof.", "Do include the pink roof.", {}),
    ("Target: -0.13", "Target 0.13", {}),
    ("Target: 0.13", "Target 0.130", {}),
    ("Scale: 1:100", "Scale 1/100", {}),
    ("Maximum height: 150 mm.", "Maximum height 150 mm.", {"scope": "Roof B"}),
    ("Maximum height: 150 mm.", "Maximum height 150 mm.", {"category": "option"}),
    ("Maximum height: 150 mm.", "Maximum height 150 mm.", {"source_id": "asset:2:attachment:0"}),
])
def test_note_recovery_cannot_change_facts_scope_category_or_email(old, new, overrides):
    manifest = [source("asset:1:attachment:0", "Roof B\n" + new),
                source("asset:2:attachment:0", "Roof B\n" + new)]
    initial = parameters(specification_request_notes=[note(old)])
    recovery = parameters(specification_request_notes=[note(new, **overrides)])
    with pytest.raises(ValueError, match="unverified source/excerpt"):
        extract_twice(initial, recovery, manifest)


def test_recovery_cannot_silently_drop_an_invalid_note():
    initial = parameters(specification_request_notes=[note("Maximum height 150 mm.")])
    with pytest.raises(ValueError, match="note index 0"):
        extract_twice(initial, parameters(), [source("asset:1:attachment:0", "No height specified")])


def test_source_link_repair_preserves_url_punctuation():
    initial = parameters(specification_request_notes=[note("https://example.test/a?x=1", category="source_link")])
    recovery = parameters(specification_request_notes=[note("https://example.test/a?x=2", category="source_link")])
    with pytest.raises(ValueError, match="unverified source/excerpt"):
        extract_twice(initial, recovery, [source("asset:1:attachment:0", "https://example.test/a?x=2")])


def test_source_link_attachment_repair_cannot_change_path_case():
    old_id, new_id = "asset:1:attachment:0", "asset:1:attachment:1"
    initial = parameters(specification_request_notes=[note("https://example.test/A", category="source_link")])
    recovery = parameters(specification_request_notes=[note("https://example.test/a", category="source_link", source_id=new_id)])
    with pytest.raises(ValueError, match="unverified source/excerpt"):
        extract_twice(initial, recovery, [source(old_id, "Signature"), source(new_id, "https://example.test/a")])


def test_request_note_alone_triggers_the_single_recovery_pass():
    initial = parameters()
    for name in type(initial).model_fields:
        if not name.startswith("specification_"):
            setattr(initial, name, "Fact")
    initial.drawing_reference = "TP12345_26.01 - B"
    initial.revision = "26.01 - B"
    values = {key.replace("U-Value", "U Value"): value for key, value in initial.as_canonical_parameters().items()}
    values["Bauder Roof Num"] = initial.bauder_roof_num
    evidence = [candidate(label, values[label], f"{label}: {values[label]}", source_id="asset:1:body",
                          request_role="direct_request")
                for label in initial.specification_assessed_fields if label != "Incoming Information"]
    initial = parameters(**{**initial.model_dump(), "specification_evidence": evidence,
                           "specification_request_notes": [note("Maximum height: 150 mm.", source_id="asset:1:body")]})
    recovery = parameters(**{**initial.model_dump(),
                            "specification_request_notes": [note("Maximum height 150 mm.", source_id="asset:1:body")]})
    text = "\n".join(e["snippet"] for e in evidence) + "\nMaximum height 150 mm."
    manifest = [source("asset:1:body", text, "email_body")]
    assert not resolve_email_evidence(extraction_payload(initial, manifest))[2]
    replies = iter([initial.model_dump(), recovery.model_dump()])
    calls = []
    def generate(model, prompt, config):
        calls.append(prompt)
        return SimpleNamespace(parsed=next(replies))
    result = LegacyGeminiClient("offline", generate_content=generate).extract_design_parameters_with_manifest(text, manifest)
    assert len(calls) == 2
    assert "Request-note diagnostics (zero-based indexes): {0:" in calls[1]
    assert resolve_request_notes(extraction_payload(result, manifest))


def test_missing_page_size_evidence_can_be_recovered_from_literal_title_block():
    text = "SCALE 1:100 @ A1"
    initial = parameters(page_size="A1")
    recovery = parameters(page_size="A1", specification_evidence=[candidate("Page Size", "A1", text)])
    result = extract_twice(initial, recovery, [source("asset:1:attachment:0", text)])
    assert result.page_size == "A1"


@pytest.mark.parametrize("absent", [None, "Not Provided"])
def test_page_size_lookup_can_be_cleared_only_with_verified_nonrequest_evidence(absent):
    text = "Unselected paper size option: A1"
    initial = parameters(page_size="A1")
    recovery = parameters(page_size=absent, specification_evidence=[candidate("Page Size", "A1", text, role="lookup")])
    result = extract_twice(initial, recovery, [source("asset:1:attachment:0", text)])
    assert result.page_size is None and result._evidence_repairs[0]["field"] == "Page Size"


def test_unsupported_page_size_still_blocks_and_logs_only_safe_role_counts():
    text = "Private client title block with no page size"
    initial = parameters(page_size="A1", specification_evidence=[candidate("Page Size", "A1", text, role="other")])
    manifest = [source("asset:1:attachment:0", text)]
    manifest[0]["pdfPageSizes"] = ["A1"]
    with pytest.raises(ValueError, match="Populated extraction has no verified applicable evidence") as error:
        extract_twice(initial, initial, manifest)
    assert "evidence_roles={'Page Size': {'other': 1}}" in str(error.value)
    assert text not in str(error.value) and "A1" not in str(error.value)


def test_note_recovery_does_not_bypass_combined_array_limit():
    notes = [note(f"Constraint {index}.") for index in range(65)]
    text = "\n".join(n["snippet"] for n in notes)
    with pytest.raises(ValueError, match="at most 64"):
        extract_twice(parameters(specification_request_notes=notes[:64]),
                      parameters(specification_request_notes=notes[64:]),
                      [source("asset:1:attachment:0", text)])
