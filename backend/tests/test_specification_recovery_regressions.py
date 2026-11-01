"""Offline regressions for Render evidence failures; no source facts are invented."""

from types import SimpleNamespace

import pytest

from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.performance_specification_evidence import (
    UNCONFIRMED_REFERENCE, VALUE_CONFLICT, resolve_email_evidence, resolve_request_notes,
)
from backend.app.services.specification_recovery import (
    extraction_payload, repair_note_list_formatting, repair_template_markers,
)
from backend.app.services.specification_semantics import (
    SpecificationEvidence, drawing_reference_key, scale_ratio, tp_reference_revision,
)
from backend.tests.test_specification_evidence import SENT, candidate, extraction, source
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


def test_literal_forwarded_header_replaces_reformatted_date_without_changing_fact():
    header = "Thursday, October 15, 2026 10:55 AM"
    text = ("From: Requester\nDate: Fri, 30 Oct 2026 11:32:17 +0000\n\nPlease revise.\n"
            f"From: Earlier requester\nSent: {header}\nSubject: Original request\n\nMinimum U-value 0.35")
    evidence = candidate("Target Min U Value", "0.35", "Minimum U-value 0.35", source_id="asset:1:body",
                         request_role="forwarded_request", message_date="Thu, 15 Oct 2026 10:55:00 +0000")
    manifest = [source("asset:1:body", text, "email_body")]
    result = extract_twice(parameters(target_min_u_value="0.35", specification_evidence=[evidence]),
                          parameters(target_min_u_value="0.35", specification_evidence=[{**evidence, "message_date": header}]), manifest)
    raw, provenance, issues = resolve_email_evidence(extraction_payload(result, manifest))
    assert not issues and raw["Target Min U Value"] == "0.35"
    assert provenance["Target Min U Value"]["selected"]["observedAt"] == "2026-10-15T09:55:00+00:00"
    assert result._evidence_repairs[0]["after"]["message_date"] == header


@pytest.mark.parametrize("case", ["different_time", "different_date", "unrelated_message", "body_date", "duplicate", "role"])
def test_date_repair_cannot_move_fact_to_another_message_or_time(case):
    header = "15 October 2026 10:55"
    text = ("From: Requester\nDate: Fri, 30 Oct 2026 11:32:17 +0000\n\nPlease revise.\n"
            f"From: Earlier requester\nSent: {header}\nSubject: Request\n\nMinimum U-value 0.35")
    if case == "unrelated_message":
        text = text.replace("Minimum U-value", "From: Different requester\nSent: 14 October 2026 10:55\n\nMinimum U-value")
    elif case == "body_date":
        text = text.replace(f"Sent: {header}\n", "").replace("Minimum U-value", f"Body starts here.\nDate: {header}\nMinimum U-value")
    elif case == "duplicate":
        text += f"\nFrom: Duplicate requester\nSent: {header}\n\nMinimum U-value 0.35"
    old_date = "2026-10-15T10:55:00Z"
    if case == "different_time":
        old_date = "2026-10-15T11:55:00Z"
    if case == "different_date":
        old_date = "2026-10-14T10:55:00Z"
    evidence = candidate("Target Min U Value", "0.35", "Minimum U-value 0.35", source_id="asset:1:body",
                         request_role="direct_request" if case == "role" else "forwarded_request", message_date=old_date)
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(parameters(target_min_u_value="0.35", specification_evidence=[evidence]),
                      parameters(target_min_u_value="0.35", specification_evidence=[{**evidence, "message_date": header}]),
                      [source("asset:1:body", text, "email_body")])


def test_iso_message_date_preserves_month_and_day():
    text = "From: Requester\nDate: 2026-10-05T10:00:00Z\nCompany: Sample Ltd"
    evidence = candidate("Company", "Sample Ltd", "Company: Sample Ltd", source_id="asset:1:body",
                         request_role="direct_request", message_date="2026-10-05T10:00:00Z")
    _, provenance, issues = resolve_email_evidence(extraction([source("asset:1:body", text, "email_body")], [evidence]))
    assert not issues and provenance["Company"]["selected"]["observedAt"] == "2026-10-05T10:00:00+00:00"


def test_recovery_can_remove_only_an_unverified_optional_note_scope():
    text = "The pink area does not require a tapered scheme."
    initial = parameters(specification_request_notes=[note(text, scope="Block C&D")])
    recovered = parameters(specification_request_notes=[note(text)])
    result = extract_twice(initial, recovered, [source("asset:1:attachment:0", text)])
    assert len(result.specification_request_notes) == 1 and result.specification_request_notes[0].scope is None
    assert result._evidence_repairs[0]["before"]["scope"] == "Block C&D"
    # If that scope was in the source, the original note must survive.
    initial = parameters(specification_request_notes=[note(text, scope="Block C&D")])
    result = extract_twice(initial, recovered, [source("asset:1:attachment:0", "Block C&D: " + text)])
    assert any(n.scope == "Block C&D" for n in result.specification_request_notes)
    assert not result._evidence_repairs


@pytest.mark.parametrize("field,value", [("Drawing Reference", "AX14992_24.06 - E"), ("Revision", "24.06 - E")])
def test_contiguous_reference_can_have_redundant_reference_parts(field, value):
    text = "AX14992_24.06 - E- Sample project - Block EF"
    evidence = candidate(field, value, text, reference_parts=["AX14992_24.06", "E"])
    raw, _, issues = resolve_email_evidence(extraction([source("asset:1:attachment:0", text)], [evidence]))
    assert not issues and raw[field] == value


@pytest.mark.parametrize("parts", [["AX14992_24.06", "F"], ["AX14992_24.07", "E"], ["AX14992_24.06 - E", "F"]])
def test_redundant_reference_parts_cannot_change_literal_suffix(parts):
    text = "AX14992_24.06 - E- Sample project - Block EF"
    evidence = candidate("Revision", "24.06 - E", text, reference_parts=parts)
    assert "Revision" in resolve_email_evidence(extraction([source("asset:1:attachment:0", text)], [evidence]))[2]


@pytest.mark.parametrize("number,self_identifying", [("AX12345_26.01 - B", True), ("TP12345_26.01 - B", True),
                                                   ("12345_26.01 - B", False), ("D.S - 12345_26.01 - B", False)])
def test_only_self_identifying_reference_can_drop_unquoted_issuer(number, self_identifying):
    evidence = candidate("Drawing Reference", number, number, issuer="Invented Ltd")
    initial = parameters(drawing_reference=number, specification_evidence=[evidence])
    recovery = parameters(drawing_reference=number, specification_evidence=[{**evidence, "issuer": None}])
    manifest = [source("asset:1:attachment:0", number)]
    result = extract_twice(initial, recovery, manifest)
    if self_identifying:
        assert result._evidence_repairs[0]["after"]["issuer"] is None
    else:
        # A bare/D.S reference is neither confirmed nor blocked by a non-TP issuer:
        # no issuer repair is claimed, and Monday CRM decides at publication.
        assert not result._evidence_repairs
        issues = resolve_email_evidence(extraction_payload(result, manifest))[2]
        assert issues["Drawing Reference"] == UNCONFIRMED_REFERENCE


def test_company_recovery_cannot_expand_brand_to_unquoted_legal_name():
    evidence = candidate("Company", "Brand Ltd", "Brand", source_id="asset:1:body", request_role="direct_request")
    with pytest.raises(ValueError, match="Raw value is absent"):
        extract_twice(parameters(company="Brand Ltd", specification_evidence=[evidence]),
                      parameters(company="Brand", specification_evidence=[{**evidence, "value": "Brand"}]),
                      [source("asset:1:body", "Brand", "email_body")])


def test_flattened_note_restores_the_entire_contiguous_bullet_passage():
    text = "  *   Area around joint (Grid H/2C-4C)\r\n     *   Do not exceed 170 mm\r\n     *   Review 1 in 80 falls."
    old = "Area around joint (Grid H/2C-4C). Do not exceed 170 mm. Review 1 in 80 falls."
    initial = parameters(specification_request_notes=[note(old)])
    manifest = [source("asset:1:attachment:0", text)]
    repair_note_list_formatting(initial, manifest)
    restored, = initial.specification_request_notes
    # All original words and the middle constraint survive; the restored excerpt
    # is genuinely contiguous in the source, including the internal bullet markers.
    assert restored.snippet in text and "Do not exceed 170 mm" in restored.snippet
    assert restored.snippet.startswith("Area around joint") and "*" in restored.snippet
    assert initial._evidence_repairs[0]["before"]["snippet"] == old
    assert resolve_request_notes(extraction_payload(initial, manifest))


@pytest.mark.parametrize("text,old", [
    ("* Do not exceed 170 mm\n  * Review 1 in 80 falls", "Do exceed 170 mm. Review 1 in 80 falls"),
    ("* Target -0.13\n  * Review 1 in 80 falls", "Target 0.13. Review 1 in 80 falls"),
    ("* Target 0.13\n  * Review 1 in 80 falls", "Target 0.130. Review 1 in 80 falls"),
    ("* Maximum 170 mm\n  * Separate restriction\n  * Review 1 in 80 falls", "Maximum 170 mm. Review 1 in 80 falls"),
    ("* Maximum 170 mm\n ...\n * Review 1 in 80 falls", "Maximum 170 mm. Review 1 in 80 falls"),
    ("* Maximum 170 mm\n  * Review 1 in 80 falls\n* Maximum 170 mm\n  * Review 1 in 80 falls", "Maximum 170 mm. Review 1 in 80 falls"),
])
def test_list_formatting_repair_rejects_changed_facts_omitted_passages_and_duplicates(text, old):
    initial = parameters(specification_request_notes=[note(old)])
    manifest = [source("asset:1:attachment:0", text)]
    repair_note_list_formatting(initial, manifest)
    assert initial.specification_request_notes[0].snippet == old and not initial._evidence_repairs


def test_list_formatting_cannot_rewrite_source_link_punctuation():
    initial = parameters(specification_request_notes=[note("https://example.test/a. More", category="source_link")])
    repair_note_list_formatting(initial, [source("asset:1:attachment:0", "https://example.test/a\n * More")])
    assert not initial._evidence_repairs


@pytest.mark.parametrize("apostrophe", ["'", "\u2019"])
def test_drawing_title_recovery_can_restore_literal_possessive_apostrophe(apostrophe):
    title = f"St John{apostrophe}s School"
    text = "Project: " + title
    old = candidate("Drawing Title", "St Johns School", text)
    result = extract_twice(parameters(drawing_title=old["value"], specification_evidence=[old]),
                          parameters(drawing_title=title, specification_evidence=[{**old, "value": title}]),
                          [source("asset:1:attachment:0", text)])
    assert result._evidence_repairs[0]["after"]["value"] == title


@pytest.mark.parametrize("old,new", [("St Johns School", "St Johns Academy"),
                                      ("Plot A1s", "Plot A1's"), ("Cant build", "Can't build"),
                                      ("Site 12", "Site 13")])
def test_title_recovery_cannot_change_words_identifiers_negations_or_numbers(old, new):
    text = "Project: " + new
    evidence = candidate("Drawing Title", old, text)
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(parameters(drawing_title=old, specification_evidence=[evidence]),
                      parameters(drawing_title=new, specification_evidence=[{**evidence, "value": new}]),
                      [source("asset:1:attachment:0", text)])


@pytest.mark.parametrize("value,ratio", [
    ("1:50", ("1", "50")), ("1/50", ("1", "50")), (" 1 / 50 ", ("1", "50")),
    ("1 to 50", ("1", "50")), ("50:1", ("50", "1")),
    ("1:50 @ A1", None), ("1to50", None), ("1.5:50", None), ("", None), (None, None),
])
def test_scale_ratio_reads_only_a_bare_ordered_ratio(value, ratio):
    assert scale_ratio(value) == ratio


@pytest.mark.parametrize("reverse", [False, True])
def test_verified_literal_scale_settles_a_reformatted_ratio_for_the_same_claim(reverse):
    # Render job 5af08583: a literal 1/50 and a CAD-style 1:50 cited the same title block.
    snippet = "scale: 1/50 @ A1"
    literal = candidate("Scale", "1/50", snippet)
    data = extraction([source(literal["source_id"], "Roof plan\n" + snippet)],
                      [{**literal, "value": "1:50"}, literal][::(-1 if reverse else 1)])
    raw, provenance, issues = resolve_email_evidence(data)
    assert raw["Scale"] == "1/50" and not issues
    assert provenance["Scale"]["rejectedCandidates"][0]["status"] == "repaired"


@pytest.mark.parametrize("change", [
    {"value": "50:1"}, {"value": "1:500"}, {"value": "1:50 @ A1"},
    {"scope": "Roof 5"}, {"source_id": "missing"},
])
def test_verified_literal_scale_does_not_hide_a_different_scale_claim(change):
    snippet = "scale: 1/50 @ A1"
    literal = candidate("Scale", "1/50", snippet)
    data = extraction([source(literal["source_id"], snippet)], [{**literal, "value": "1:50", **change}, literal])
    _, provenance, issues = resolve_email_evidence(data)
    assert "Scale" in issues
    assert provenance["Scale"]["rejectedCandidates"][0]["status"] == "unverified"


def test_scale_recovery_replaces_a_reformatted_ratio_with_its_literal_and_is_audited():
    snippet = "scale: 1/50 @ A1"
    manifest = [source("asset:1:attachment:0", snippet)]
    result = extract_twice(parameters(scale="1:50", specification_evidence=[candidate("Scale", "1:50", snippet)]),
                           parameters(scale="1/50", specification_evidence=[candidate("Scale", "1/50", snippet)]),
                           manifest)
    raw, _, issues = resolve_email_evidence(extraction_payload(result, manifest))
    assert raw["Scale"] == "1/50" and not issues
    assert [e.value for e in result.specification_evidence if e.field == "Scale"] == ["1/50"]
    repair, = result._evidence_repairs
    assert (repair["before"]["value"], repair["after"]["value"]) == ("1:50", "1/50")


@pytest.mark.parametrize("recovered", ["50/1", "1/500"])
def test_scale_recovery_cannot_change_the_ratio(recovered):
    text = f"scale: {recovered} @ A1"
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract_twice(parameters(scale="1:50", specification_evidence=[candidate("Scale", "1:50", text)]),
                      parameters(scale=recovered, specification_evidence=[candidate("Scale", recovered, text)]),
                      [source("asset:1:attachment:0", text)])


def kent_drawing_titles():
    """Render job 18700b4f: two roof titles, repeated by a later forwarded email."""
    first, second = "asset:288731044", "asset:288785101"
    sources = [source(f"{first}:body", "From: Sales\nDate: Tue, 27 Oct 2026 14:43:13 +0000\n\nForwarded enquiry",
                      "email_body")]
    evidence = []
    for index, roof in enumerate(("Darwin College", "Registry Roof")):
        title, postcode = "University of Kent, " + roof, ("CT2 7NY", "CT2 7NZ")[index]
        plan = source(f"{first}:attachment:{index + 2}", f"Roof Plan \u2013 {title}, {postcode}")
        outlet = source(f"{second}:attachment:{index}", f"University of Kent\n27/10/26\nOutlet Plan\n{roof}")
        document = candidate("Drawing Title", title, plan["text"], source_id=plan["sourceId"], scope=roof)
        evidence += [
            document,
            # Neither the forwarded date nor the comma-joined title-block lines verify.
            {**document, "request_role": "forwarded_request", "message_date": "27 October 2026 14:38"},
            {**document, "source_id": outlet["sourceId"], "snippet": outlet["text"]},
        ]
        sources += [plan, outlet]
    return sources, evidence


def test_unverified_duplicates_of_an_undated_verified_title_are_redundant():
    sources, evidence = kent_drawing_titles()
    raw, provenance, issues = resolve_email_evidence(extraction(sources, evidence))
    assert raw["Drawing Title"] is None and issues == {"Drawing Title": VALUE_CONFLICT}
    rejected = provenance["Drawing Title"]["rejectedCandidates"]
    assert sorted(e["reason"] for e in rejected) == 2 * ["Candidate request timestamp could not be verified"] + 2 * [
        "Raw value is absent from the supporting excerpt"]
    assert {e["status"] for e in rejected} == {"redundant"}
    raw, _, issues = resolve_email_evidence(extraction(sources, evidence), ["Darwin College"])
    assert raw["Drawing Title"] == "University of Kent, Darwin College" and not issues


DUPLICATE_PLAN = "Roof Plan \u2013 Example Campus, North Roof"
DUPLICATE_OUTLET = "Example Campus\n27/10/26\nOutlet Plan\nNorth Roof"


@pytest.mark.parametrize("verified_change,duplicate_change,outlet_text", [
    ({}, {"value": "Example Campus, South Roof"}, DUPLICATE_OUTLET),
    ({}, {"scope": "South Roof"}, DUPLICATE_OUTLET),
    ({}, {"issuer": "Architect"}, DUPLICATE_OUTLET),
    ({}, {}, "North Roof\nOutlet Plan\nExample Campus"),  # Words out of order.
    ({}, {}, "Example Campus\nOutlet Plan"),  # A value word is absent.
    ({}, {"snippet": "Unlisted excerpt"}, DUPLICATE_OUTLET),
    ({"message_date": SENT}, {}, DUPLICATE_OUTLET),  # The verified twin is dated.
    ({"message_date": SENT}, {"source_id": "asset:1:attachment:0", "snippet": DUPLICATE_PLAN,
                              "request_role": "forwarded_request", "message_date": "27 October 2026 14:38"},
     DUPLICATE_OUTLET),
])
def test_only_an_undated_verified_twin_makes_an_unverified_duplicate_redundant(
        verified_change, duplicate_change, outlet_text):
    verified = candidate("Drawing Title", "Example Campus, North Roof", DUPLICATE_PLAN,
                         scope="North Roof", **verified_change)
    duplicate = {**verified, "source_id": "asset:2:attachment:0", "snippet": outlet_text,
                 "message_date": None, **duplicate_change}
    data = extraction([source("asset:1:body", f"From: Requester\nDate: {SENT}\n\nRequest", "email_body"),
                       source("asset:1:attachment:0", DUPLICATE_PLAN), source("asset:2:attachment:0", outlet_text)],
                      [verified, duplicate])
    _, provenance, issues = resolve_email_evidence(data)
    assert len(provenance["Drawing Title"]["candidates"]) == 1 and "Drawing Title" in issues
    assert provenance["Drawing Title"]["rejectedCandidates"][0]["status"] == "unverified"


def test_failure_names_unverified_candidates_masked_by_a_conflict():
    texts = {f"asset:1:attachment:{index}": f"Project: {roof} Roof"
             for index, roof in enumerate(("Alpha", "Beta", "Gamma"))}
    evidence = [candidate("Drawing Title", text.removeprefix("Project: "), text, source_id=identity,
                          scope=text.split()[1]) for identity, text in texts.items()]
    evidence[2] = {**evidence[2], "request_role": "forwarded_request", "message_date": "27 October 2026 14:38"}
    initial = parameters(specification_evidence=evidence)
    with pytest.raises(ValueError, match="Conflicting roof/drawing/request values") as error:
        extract_twice(initial, initial, [source(identity, text) for identity, text in texts.items()])
    assert ("unverified_candidates={'Drawing Title': ['Candidate request timestamp could not be verified']}"
            in str(error.value))
    assert "Gamma" not in str(error.value) and "14:38" not in str(error.value)


EN_DASH_EMAIL = "Please proceed with TP18954_26.03 \u2013 A as reduced insulation thickness."


@pytest.mark.parametrize("dash", ["\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212"])
def test_typographic_dash_in_a_reference_is_the_standard_hyphen(dash):
    reference = f"TP18954_26.03 {dash} A"
    assert tp_reference_revision(reference) == "26.03 - A"
    assert tp_reference_revision(f"D.S {dash} 18954_26.03 {dash} A", confirmed_crm=True) == "26.03 - A"
    assert drawing_reference_key(reference) == drawing_reference_key("TP18954_26.03 - A")


def test_en_dash_email_reference_resolves_to_the_standard_reference_and_revision():
    # Render job feffdc95: Outlook typeset the TaperedPlus reference with an en dash.
    reference = candidate("Drawing Reference", "TP18954_26.03 \u2013 A", EN_DASH_EMAIL, issuer="TaperedPlus")
    revision = candidate("Revision", "26.03 \u2013 A", EN_DASH_EMAIL, issuer="TaperedPlus")
    raw, provenance, issues = resolve_email_evidence(
        extraction([source("asset:1:attachment:0", EN_DASH_EMAIL)], [reference, revision]))
    assert (raw["Drawing Reference"], raw["Revision"]) == ("TP18954_26.03 - A", "26.03 - A")
    assert "Drawing Reference" not in issues and "Revision" not in issues
    assert provenance["Drawing Reference"]["selected"]["value"] == "TP18954_26.03 \u2013 A"


@pytest.mark.parametrize("value,verified", [
    ("TP18954_26.03 - A", True), ("TP18954_26.03 \u2013 B", False), ("TP18954_26.04 - A", False),
])
def test_reference_dash_equivalence_never_changes_the_number_or_letter(value, verified):
    data = extraction([source("asset:1:attachment:0", EN_DASH_EMAIL)],
                      [candidate("Drawing Reference", value, EN_DASH_EMAIL)])
    raw, _, issues = resolve_email_evidence(data)
    assert (raw["Drawing Reference"] == "TP18954_26.03 - A") is verified
    assert ("Drawing Reference" in issues) is not verified


def test_reference_excerpt_must_still_match_its_source_literally():
    snippet = EN_DASH_EMAIL.replace("\u2013", "-")
    data = extraction([source("asset:1:attachment:0", EN_DASH_EMAIL)],
                      [candidate("Drawing Reference", "TP18954_26.03 - A", snippet)])
    assert resolve_email_evidence(data)[2]["Drawing Reference"] == "Supporting source/excerpt could not be verified"


def test_reference_recovery_may_restate_an_en_dash_with_the_standard_hyphen():
    manifest = [source("asset:1:attachment:0", EN_DASH_EMAIL)]
    old = candidate("Drawing Reference", "TP18954_26.03 \u2013 A", EN_DASH_EMAIL + " Unverified tail")
    new = candidate("Drawing Reference", "TP18954_26.03 - A", EN_DASH_EMAIL)
    result = extract_twice(parameters(drawing_reference=old["value"], specification_evidence=[old]),
                           parameters(drawing_reference=new["value"], specification_evidence=[new]), manifest)
    raw, _, issues = resolve_email_evidence(extraction_payload(result, manifest))
    assert raw["Drawing Reference"] == "TP18954_26.03 - A" and "Drawing Reference" not in issues
    repair, = result._evidence_repairs
    assert (repair["before"]["value"], repair["after"]["value"]) == (old["value"], new["value"])


DS_TEXT = "Reference: D.S - 00121_26.01 - D"


@pytest.mark.parametrize("issuer", ["AXTER", None])
def test_literal_ds_reference_without_tp_issuer_awaits_crm_confirmation(issuer):
    # Render job e30fd92c: an AXTER-labelled D.S reference cannot be confirmed by extraction.
    data = extraction([source("asset:1:attachment:0", DS_TEXT)],
                      [candidate("Drawing Reference", "D.S - 00121_26.01 \u2013 D", DS_TEXT, issuer=issuer)])
    raw, provenance, issues = resolve_email_evidence(data)
    assert raw["Drawing Reference"] == "D.S - 00121_26.01 - D"
    assert issues["Drawing Reference"] == UNCONFIRMED_REFERENCE
    assert provenance["Drawing Reference"]["selected"]["crmConfirmation"] == "required"
    assert "rejectedCandidates" not in provenance["Drawing Reference"]


@pytest.mark.parametrize("value,issuer,reason", [
    ("D.S - 00121_26.01 - D", "TaperedPlus", "Drawing Reference is not verified as TaperedPlus-issued"),
    ("00121", None, "Drawing Reference is not verified as TaperedPlus-issued"),
    ("D.S - 00121_26.02 - D", None, "Raw value is absent from the supporting excerpt"),
])
def test_ds_reference_still_rejects_unsupported_claims(value, issuer, reason):
    data = extraction([source("asset:1:attachment:0", DS_TEXT)],
                      [candidate("Drawing Reference", value, DS_TEXT, issuer=issuer)])
    _, provenance, issues = resolve_email_evidence(data)
    assert issues["Drawing Reference"] == reason
    assert provenance["Drawing Reference"]["rejectedCandidates"][0]["status"] == "unverified"


def test_tp_evidenced_ds_reference_needs_no_crm_confirmation():
    text = "Tapered Plus drawing D.S - 00121_26.01 - D"
    data = extraction([source("asset:1:attachment:0", text)],
                      [candidate("Drawing Reference", "D.S - 00121_26.01 - D", text, issuer="TaperedPlus")])
    raw, _, issues = resolve_email_evidence(data)
    assert raw["Drawing Reference"] == "D.S - 00121_26.01 - D" and "Drawing Reference" not in issues


def test_unconfirmed_ds_reference_does_not_fail_extraction():
    evidence = candidate("Drawing Reference", "D.S - 00121_26.01 - D", DS_TEXT, issuer="AXTER")
    manifest = [source("asset:1:attachment:0", DS_TEXT)]
    initial = parameters(drawing_reference=evidence["value"], specification_evidence=[evidence])
    result = extract_twice(initial, initial, manifest)
    assert resolve_email_evidence(extraction_payload(result, manifest))[2]["Drawing Reference"] == UNCONFIRMED_REFERENCE


U_VALUE_NOTE = "Notes: Falls Updated to 1:60 and 1:40, U-Value Changed to 0.15W/m\u00b2K"


@pytest.mark.parametrize("value,text", [
    ("0.15 W/m\u00b2K", U_VALUE_NOTE),  # Render job 9f990ae5.
    ("0.15W/m\u00b2K", "U-Value: 0.15 W/m\u00b2K"),
    ("0.15", "U-Value: 0.15 W/m\u00b2K"),  # A bare number remains a whole word.
    ("0.18", "U-Value*: 0.22 W/m\u00b2K (0.18 Target)"),
])
def test_u_value_number_unit_spacing_is_presentation(value, text):
    data = extraction([source("asset:1:attachment:0", text)], [candidate("Target U Value", value, text)])
    raw, _, issues = resolve_email_evidence(data)
    assert raw["Target U Value"] == value and "Target U Value" not in issues


@pytest.mark.parametrize("value", ["0.16 W/m\u00b2K", "0.1 5W/m\u00b2K", "0.15 W/mK", "0.15 W/m\u00b2K (target)"])
def test_u_value_spacing_tolerance_never_changes_the_number_or_unit(value):
    data = extraction([source("asset:1:attachment:0", U_VALUE_NOTE)], [candidate("Target U Value", value, U_VALUE_NOTE)])
    assert "Target U Value" in resolve_email_evidence(data)[2]


def test_spacing_variants_of_one_u_value_are_not_a_conflict():
    data = extraction([source("asset:1:attachment:0", U_VALUE_NOTE)], [
        candidate("Target U Value", "0.15 W/m\u00b2K", U_VALUE_NOTE, issuer="AXTER"),
        candidate("Target U Value", "0.15W/m\u00b2K", U_VALUE_NOTE),
    ])
    raw, _, issues = resolve_email_evidence(data)
    assert raw["Target U Value"] == "0.15 W/m\u00b2K" and "Target U Value" not in issues


def test_u_value_recovery_may_restate_unit_spacing_and_is_audited():
    manifest = [source("asset:1:attachment:0", U_VALUE_NOTE)]
    old = candidate("Target U Value", "0.15 W/m\u00b2K", U_VALUE_NOTE + " Unverified tail")
    new = candidate("Target U Value", "0.15W/m\u00b2K", U_VALUE_NOTE)
    result = extract_twice(parameters(target_u_value=old["value"], specification_evidence=[old]),
                           parameters(target_u_value=new["value"], specification_evidence=[new]), manifest)
    raw, _, issues = resolve_email_evidence(extraction_payload(result, manifest))
    assert raw["Target U Value"] == "0.15W/m\u00b2K" and "Target U Value" not in issues
    repair, = result._evidence_repairs
    assert (repair["before"]["value"], repair["after"]["value"]) == (old["value"], new["value"])


J42_TEXT = r"1.8. Waterproof covering: Polyester reinforced \*\*\*PVC/ FPO\*\*\* single ply membrane."
J42_LITERAL = "Polyester reinforced ***PVC/ FPO*** single ply membrane"


@pytest.mark.parametrize("snippet", [
    "Waterproof covering: Polyester reinforced ***PVC/ FPO*** single ply membrane.",
    r"Waterproof covering: Polyester reinforced \*\*\*PVC/ FPO\*\*\* single ply membrane.",
])
def test_dropped_template_markers_are_restored_from_the_cited_excerpt(snippet):
    # Render job 84ec79b2: the response removed *** from a specification template value.
    evidence = candidate("Membrane", "Polyester reinforced PVC/ FPO single ply membrane", snippet)
    manifest = [source("asset:1:attachment:0", J42_TEXT)]
    initial = parameters(membrane=evidence["value"], specification_evidence=[evidence])
    result = extract_twice(initial, initial, manifest)
    raw, _, issues = resolve_email_evidence(extraction_payload(result, manifest))
    assert raw["Membrane"] == J42_LITERAL and "Membrane" not in issues
    repair = next(r for r in result._evidence_repairs if r["reason"] == "Verified literal template-marker repair")
    assert (repair["before"]["value"], repair["after"]["value"]) == (evidence["value"], J42_LITERAL)
    assert repair["after"]["snippet"] == snippet and repair["contentSha256"] == "abc"


@pytest.mark.parametrize("value,snippet", [
    ("Polyester reinforced PVC single ply membrane", J42_LITERAL),  # A word was removed.
    ("Polyester reinforced FPO/ PVC single ply membrane", J42_LITERAL),  # Words were reordered.
    ("Polyester PVC/ FPO single ply", "Polyester ***PVC/ FPO single ply"),  # Unbalanced markers.
    ("PVC/ FPO single ply", "***PVC/ FPO*** single ply; ***PVC/ FPO*** single ply"),  # Ambiguous passage.
])
def test_template_marker_repair_never_changes_words_or_guesses(value, snippet):
    result = SimpleNamespace(specification_evidence=[SpecificationEvidence.model_validate(
        candidate("Membrane", value, snippet))], _evidence_repairs=[])
    repair_template_markers(result, [source("asset:1:attachment:0", snippet)])
    assert result.specification_evidence[0].value == value and not result._evidence_repairs
