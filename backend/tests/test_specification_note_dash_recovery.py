"""Prose dash recovery restores literal same-source notes without changing facts."""

from types import SimpleNamespace

import pytest

from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.performance_specification_evidence import request_note_diagnostics, resolve_request_notes
from backend.app.services.specification_recovery import extraction_payload, repair_note_prose_dashes
from backend.tests.test_specification_evidence import source
from backend.tests.test_specification_publication_hardening import extract_twice, parameters


SOURCE_ID = "asset:1:body"


def note(snippet, **overrides):
    return {"source_id": SOURCE_ID, "snippet": snippet, "scope": None, "category": "option", **overrides}


def repair(snippet, text, *, note_overrides=None, source_overrides=None, extra_sources=()):
    result = parameters(specification_request_notes=[note(snippet, **(note_overrides or {}))])
    entry = {**source(SOURCE_ID, text, "email_body"), **(source_overrides or {})}
    manifest = [entry, *extra_sources]
    repair_note_prose_dashes(result, manifest)
    return result, manifest


@pytest.mark.parametrize("source_dash,model_dash", [
    ("-", "\u2013"), ("-", "\u2014"), ("\u2013", "-"), ("\u2014", "\u2013"),
])
def test_verified_prose_dash_restores_exact_source_words_and_audits(source_dash, model_dash):
    literal = f"The budget is tight {source_dash} could the green roof be installed later?"
    rejected = f"The budget is tight {model_dash} could the green roof be installed later?"
    result, manifest = repair(rejected, "Roof A\n" + literal, note_overrides={"scope": "Roof A"})

    repaired, = result.specification_request_notes
    assert repaired.snippet == literal and repaired.source_id == SOURCE_ID
    assert repaired.category == "option" and repaired.scope == "Roof A"
    assert not request_note_diagnostics(extraction_payload(result, manifest))
    audit, = result._evidence_repairs
    assert audit["before"]["snippet"] == rejected
    assert audit["after"] == repaired.model_dump()
    assert audit["contentSha256"] == "abc"


def test_prose_dash_repair_keeps_literal_whitespace_and_case():
    literal = "Budget is TIGHT  -\n could the green roof be installed later?"
    result, _ = repair("Budget is tight \u2013 could the green roof be installed later?", literal)
    assert result.specification_request_notes[0].snippet == literal


def test_note_only_prose_dash_repair_needs_no_model_recovery_call():
    literal = "The budget is tight - could the green roof be installed later?"
    initial = parameters(specification_request_notes=[note(literal.replace(" - ", " \u2013 "))])
    calls = []

    def generate(model, prompt, config):
        calls.append(prompt)
        return SimpleNamespace(parsed=initial.model_dump())

    result = LegacyGeminiClient("offline", generate_content=generate).extract_design_parameters_with_manifest(
        "Synthetic source context", [source(SOURCE_ID, literal, "email_body")],
    )
    assert len(calls) == 1
    assert result.specification_request_notes[0].snippet == literal


def test_repaired_invalid_note_and_existing_correct_note_collapse_during_recovery():
    literal = "The budget is tight - could the green roof be installed later?"
    unrelated = "Do not include the pink roof."
    initial = parameters(specification_request_notes=[
        note(literal.replace(" - ", " \u2013 ")), note(literal), note(unrelated, category="exclusion"),
    ])
    recovered = parameters(specification_request_notes=[note(literal)])
    manifest = [source(SOURCE_ID, literal + "\n" + unrelated, "email_body")]

    result = extract_twice(initial, recovered, manifest)
    notes = resolve_request_notes(extraction_payload(result, manifest))
    assert [entry["snippet"] for entry in notes].count(literal) == 1
    assert {entry["snippet"] for entry in notes} == {literal, unrelated}
    assert len(result.specification_request_notes) == 2
    assert len(result._evidence_repairs) == 1


@pytest.mark.parametrize("literal,rejected", [
    ("Budget is tight - do not include sedum.", "Budget is tight \u2013 do include sedum."),
    ("Budget is tight - maximum 120 mm.", "Budget is tight \u2013 maximum 150 mm."),
    ("Budget is tight - include only the rear roof.", "Budget is tight \u2013 include the rear roof."),
    ("Budget is tight - include the rear roof.", "Budget is tight \u2013 include only the rear roof."),
    ("Budget is tight - exclude roof. Further restriction. Review falls.",
     "Budget is tight \u2013 exclude roof. Review falls."),
    ("Target - 0.13", "Target \u2013 0.13"),
    ("Allow 120 - 150 mm", "Allow 120 \u2013 150 mm"),
    ("Use tissue-faced insulation", "Use tissue\u2013faced insulation"),
    ("Use specification AX-123", "Use specification AX\u2013123"),
    ("Roof A - B", "Roof A \u2013 B"),
    ("Join AB - CD", "Join AB \u2013 CD"),
    ("Join AB - CD", "Join ab \u2013 cd"),
    ("Join TP123AB - CD456", "Join TP123AB \u2013 CD456"),
    ("Join TP123ab - cd", "Join TP123ab \u2013 cd"),
    ("Join ab - cd456", "Join ab \u2013 cd456"),
    ("Budget is tight - \u0130t can be installed later.", "Budget is tight \u2013 it can be installed later."),
    ("Budget is tight - retain -0.13.", "Budget is tight \u2013 retain 0.13."),
])
def test_prose_dash_repair_cannot_change_words_numbers_signs_or_identifiers(literal, rejected):
    result, manifest = repair(rejected, literal)
    assert result.specification_request_notes[0].snippet == rejected
    assert not result._evidence_repairs
    assert request_note_diagnostics(extraction_payload(result, manifest))


@pytest.mark.parametrize("category", ["source_link", "constraint"])
def test_prose_dash_repair_never_rewrites_notes_containing_urls(category):
    literal = "Budget is tight - review https://example.test/roof-a?x=1."
    rejected = literal.replace(" - ", " \u2013 ")
    result, manifest = repair(rejected, literal, note_overrides={"category": category})
    assert result.specification_request_notes[0].snippet == rejected
    assert not result._evidence_repairs
    assert request_note_diagnostics(extraction_payload(result, manifest))


@pytest.mark.parametrize("case", ["missing_source", "unreadable_source", "other_email", "other_attachment"])
def test_prose_dash_repair_stays_with_the_same_readable_cited_source(case):
    literal = "The budget is tight - could the green roof be installed later?"
    rejected = literal.replace(" - ", " \u2013 ")
    overrides = {"status": "failed:SourceDocumentError"} if case == "unreadable_source" else {}
    text = literal if case in {"missing_source", "unreadable_source"} else "Unrelated passage"
    note_overrides = {"source_id": "missing"} if case == "missing_source" else {}
    alternate_id = "asset:2:body" if case == "other_email" else "asset:1:attachment:0"
    result, manifest = repair(rejected, text, note_overrides=note_overrides, source_overrides=overrides,
                              extra_sources=[source(alternate_id, literal)])
    assert result.specification_request_notes[0].snippet == rejected
    assert not result._evidence_repairs
    assert request_note_diagnostics(extraction_payload(result, manifest))


def test_prose_dash_repair_does_not_choose_between_duplicate_passages():
    literal = "The budget is tight - could the green roof be installed later?"
    rejected = literal.replace(" - ", " \u2013 ")
    result, manifest = repair(rejected, literal + "\nOther roof\n" + literal)
    assert result.specification_request_notes[0].snippet == rejected
    assert not result._evidence_repairs
    assert request_note_diagnostics(extraction_payload(result, manifest))


def test_prose_dash_repair_uses_html_literal_text_without_matching_visual_prose():
    literal = "The budget is tight - could the green roof be installed later?"
    rejected = literal.replace(" - ", " \u2013 ")
    result, manifest = repair(rejected, "Plain text omitted this passage", source_overrides={"emailHtmlText": literal})
    assert result.specification_request_notes[0].snippet == literal
    assert not request_note_diagnostics(extraction_payload(result, manifest))

    image_result, image_manifest = repair(rejected, "[TEXT REGION 1]\nRoof plan\n[/TEXT REGION 1]\n"
                                         "VISUAL CONTEXT\n" + literal,
                                         source_overrides={"imageTextRegions": [{"id": "1", "text": "Roof plan"}]})
    assert image_result.specification_request_notes[0].snippet == rejected
    assert not image_result._evidence_repairs
    assert request_note_diagnostics(extraction_payload(image_result, image_manifest))


def test_identical_plain_and_html_passages_do_not_create_false_ambiguity():
    literal = "The budget is tight - could the green roof be installed later?"
    result, _ = repair(literal.replace(" - ", " \u2013 "), literal,
                       source_overrides={"emailHtmlText": literal})
    assert result.specification_request_notes[0].snippet == literal


@pytest.mark.parametrize("regions", [1, 2])
def test_image_prose_dash_repair_requires_one_literal_region_match(regions):
    literal = "The budget is tight - could the green roof be installed later?"
    rejected = literal.replace(" - ", " \u2013 ")
    text = "\n".join(f"[TEXT REGION {index}]\n{literal}\n[/TEXT REGION {index}]"
                     for index in range(1, regions + 1))
    result, manifest = repair(rejected, text, source_overrides={
        "sourceType": "attachment",
        "imageTextRegions": [{"id": str(index), "text": literal} for index in range(1, regions + 1)],
    })
    if regions == 1:
        assert result.specification_request_notes[0].snippet == literal
        assert not request_note_diagnostics(extraction_payload(result, manifest))
    else:
        assert result.specification_request_notes[0].snippet == rejected
        assert not result._evidence_repairs
        assert request_note_diagnostics(extraction_payload(result, manifest))


def test_different_literal_dash_variants_do_not_choose_a_source_wording():
    literal = "The budget is tight - could the green roof be installed later?"
    rejected = literal.replace(" - ", " \u2013 ")
    result, manifest = repair(rejected, literal, source_overrides={
        "emailHtmlText": literal.replace(" - ", " \u2014 "),
    })
    assert result.specification_request_notes[0].snippet == rejected
    assert not result._evidence_repairs
    assert request_note_diagnostics(extraction_payload(result, manifest))


def test_verified_original_note_is_not_rewritten():
    literal = "The budget is tight \u2013 could the green roof be installed later?"
    result, manifest = repair(literal, literal)
    assert result.specification_request_notes[0].snippet == literal
    assert not result._evidence_repairs
    assert not request_note_diagnostics(extraction_payload(result, manifest))


def test_prose_dash_repair_does_not_erase_an_unverified_scope():
    literal = "The budget is tight - could the green roof be installed later?"
    rejected = literal.replace(" - ", " \u2013 ")
    result, manifest = repair(rejected, literal, note_overrides={"scope": "Invented roof"})
    assert result.specification_request_notes[0].snippet == rejected
    assert result.specification_request_notes[0].scope == "Invented roof"
    assert not result._evidence_repairs
    assert request_note_diagnostics(extraction_payload(result, manifest))
