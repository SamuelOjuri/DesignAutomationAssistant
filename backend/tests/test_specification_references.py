"""Reference identities and latest recommendations never imply a future issue."""

import pytest

from backend.app.services.specification_references import (
    DrawingReference, drawing_reference_key, drawing_references_equal,
    find_references, parse_drawing_reference, parse_project_reference,
    recommend_drawing_candidates, revision_rank,
)


def drawing(identity, reference, *, state="active", **extra):
    return {"id": str(identity), "name": reference, "state": state,
            "board": {"id": "drawings"}, **extra}


@pytest.mark.parametrize("prefix", ["TP", "AX", "AB", "CD", "ZZ", "fa"])
@pytest.mark.parametrize("dash", ["-", "\u2013", "\u2014", "\u2212"])
def test_all_two_letter_prefixes_preserve_source_identity(prefix, dash):
    literal = f"{prefix} 00123_26.01 {dash} AA"
    parsed = parse_drawing_reference(literal)
    assert parsed.literal == literal
    assert parsed.prefix == prefix.upper()
    assert parsed.project_number == "00123"
    assert parsed.project_key == prefix.upper() + "00123"
    assert parsed.suffix == "26.01 - AA"
    assert parsed.components == (26, 1)
    assert parsed.revision == "AA"


@pytest.mark.parametrize("reference", ["123_26.01 - A", "D.S - 00123_26.01 - B"])
def test_legacy_references_require_explicit_confirmation(reference):
    assert parse_drawing_reference(reference) is None
    assert parse_drawing_reference(reference, confirmed_legacy=True).literal == reference


@pytest.mark.parametrize("value", ["ABC123_26.01 - A", "A123_26.01 - A", "AB123", "AB123_26", "AB123_26.01 - 2", "AB123_26.01 - A extra"])
def test_other_identifier_shapes_are_not_silently_repaired(value):
    assert parse_drawing_reference(value) is None


def test_keys_do_not_strip_prefix_or_project_leading_zeros():
    keys = {drawing_reference_key(value) for value in [
        "AB00123_26.01 - A", "CD00123_26.01 - A", "AB123_26.01 - A", "00123_26.01 - A",
    ]}
    assert len(keys) == 4
    assert drawing_reference_key("ab 00123_26.01\u2013a") == drawing_reference_key("AB00123_26.01 - A")


def test_legacy_alias_requires_verified_printed_project_identity():
    assert not drawing_references_equal("TP00123_26.01 - A", "00123_26.01 - A")
    assert drawing_references_equal("TP00123_26.01 - A", "00123_26.01 - A",
                                    confirmed_project_reference="TP00123")
    assert not drawing_references_equal("AB00123_26.01 - A", "CD00123_26.01 - A",
                                        confirmed_project_reference="AB00123")
    assert not drawing_references_equal("TP123_26.01 - A", "00123_26.01 - A",
                                        confirmed_project_reference="TP00123")


def test_inventory_keeps_literal_offsets_and_separates_project_from_item_id():
    text = "Subject: Re AB 00123; drawing cd00124_26.01 \u2013 AA, Monday item 987654321."
    matches = find_references(text)
    assert [match.reference.project_key for match in matches] == ["AB00123", "CD00124"]
    assert [text[match.start:match.end] for match in matches] == ["AB 00123", "cd00124_26.01 \u2013 AA"]
    assert isinstance(matches[1].reference, DrawingReference)
    assert parse_project_reference("AB 00123").key == "AB00123"
    assert not find_references("The legacy 00123_26.01 - A needs confirmation")


def test_project_inventory_does_not_interpret_header_words_dates_or_domains_as_references():
    text = "Sent at 15:30 on 29 October 2026\nFrom: Client <client@example.co.uk>\nexample.co.uk 29 October\nIn 2026 we 12 saw an 18."
    assert find_references(text) == []


def test_ambiguous_word_prefix_remains_available_with_explicit_reference_context():
    text = "Reference: AT 15\nproject ref in 2026\nAT15 and ab 00123"
    assert [match.reference.project_key for match in find_references(text)] == ["AT15", "IN2026", "AT15", "AB00123"]


def test_revision_order_handles_multi_letter_issues():
    assert [revision_rank(value) for value in [None, "Y", "Z", "AA", "AB"]] == [0, 25, 26, 27, 28]


def test_exact_relevant_target_wins_over_newer_general_fallback():
    result = recommend_drawing_candidates([
        drawing("900", "AB00123_26.01 - Y"), drawing("2", "AB00123_26.01 - AA"),
    ], requested_reference="ab 00123_26.01 \u2013 Y", project_reference="AB00123")
    assert result["selectedItemId"] == "900" and result["status"] == "exact"
    assert result["recommendations"][0]["itemId"] == "2"
    assert "historical" in result["issues"][0]


def test_latest_existing_drawings_are_ranked_numerically_with_each_roof_retained():
    result = recommend_drawing_candidates([
        drawing("9999", "AB00123_9.01 - AB"), drawing("2", "AB00123_10.01 - Z"),
        drawing("1", "AB00123_10.01 - AA"), drawing("7", "AB00123_10.02 - B"),
        drawing("3", "CD00123_11.01 - A"),
    ], project_reference="AB00123")
    assert result["status"] == "ambiguous" and result["selectedItemId"] is None
    assert {candidate["itemId"] for candidate in result["recommendations"]} == {"1", "7"}
    assert len(result["candidates"]) == 5


def test_archived_target_offers_latest_same_series_without_cross_roof_selection():
    result = recommend_drawing_candidates([
        drawing("1", "TP00123_25.01 - A", state="archived"),
        drawing("2", "TP00123_26.01 - AA"), drawing("3", "TP00123_26.01 - AB"),
        drawing("9", "TP00123_27.02 - Z"),
    ], requested_reference="TP00123_25.01 - A", project_reference="TP00123")
    assert result["status"] == "fallback" and result["selectedItemId"] == "3"
    assert len(result["recommendations"]) == 2
    assert "archived" in result["issues"][0]


def test_known_target_never_falls_back_to_different_series():
    result = recommend_drawing_candidates([drawing("9", "AB00123_27.02 - Z")],
                                          requested_reference="AB00123_26.01 - A")
    assert result["status"] == "unmatched" and result["selectedItemId"] is None


def test_same_reference_duplicate_records_are_reviewable_not_id_ranked():
    result = recommend_drawing_candidates([
        drawing("1", "AB00123_26.01 - AA"), drawing("99999", "AB00123_26.01 - AA"),
    ], requested_reference="AB00123_26.01 - AA")
    assert result["status"] == "ambiguous" and result["selectedItemId"] is None


@pytest.mark.parametrize("state", ["archived", "deleted", None])
def test_all_inactive_or_unreadable_records_never_select_highest_id(state):
    result = recommend_drawing_candidates([drawing("99999", "AB00123_26.01 - Z", state=state)])
    assert result["status"] == "no_active" and result["selectedItemId"] is None
    assert result["recommendations"] == []


def test_unavailable_is_distinct_from_valid_empty_inventory():
    assert recommend_drawing_candidates([], unavailable=True)["status"] == "unavailable"
    assert recommend_drawing_candidates([])["status"] == "empty"


def test_explicit_roof_scopes_remain_separate_even_with_same_numeric_series():
    result = recommend_drawing_candidates([
        drawing("1", "AB00123_26.01 - A", scope="North roof"),
        drawing("2", "AB00123_26.01 - B", scope="South roof"),
    ])
    assert result["selectedItemId"] is None and len(result["recommendations"]) == 2
    scoped = recommend_drawing_candidates([
        drawing("1", "AB00123_26.01 - A", scope="North roof"),
        drawing("2", "AB00123_26.01 - B", scope="South roof"),
    ], scope="North roof")
    assert scoped["selectedItemId"] == "1"


def test_malformed_requested_target_never_selects_a_generic_fallback():
    result = recommend_drawing_candidates([drawing("1", "AB00123_26.01 - A")],
                                          requested_reference="other-identifier")
    assert result["status"] == "unmatched" and result["selectedItemId"] is None
    assert result["recommendations"][0]["itemId"] == "1"


def test_confirmed_legacy_crm_name_is_an_exact_target_without_inventing_prefix():
    result = recommend_drawing_candidates([drawing("1", "00123_26.01 - A")],
                                          requested_reference="TP00123_26.01 - A",
                                          project_reference="TP00123")
    assert result["status"] == "exact" and result["selectedItemId"] == "1"
    assert result["candidates"][0]["reference"] == "00123_26.01 - A"


def test_confirmed_legacy_fallback_stays_with_same_project_and_roof():
    result = recommend_drawing_candidates([drawing("1", "00123_26.01 - B")],
                                          requested_reference="TP00123_25.01 - A",
                                          project_reference="TP00123")
    assert result["status"] == "fallback" and result["selectedItemId"] == "1"


def test_explicit_series_label_cannot_alias_different_prefixed_projects():
    result = recommend_drawing_candidates([
        drawing("1", "AB00123_26.01 - A", series="North roof"),
        drawing("2", "CD00123_26.01 - B", series="North roof"),
    ])
    assert result["status"] == "ambiguous" and result["selectedItemId"] is None
    assert {candidate["itemId"] for candidate in result["recommendations"]} == {"1", "2"}
