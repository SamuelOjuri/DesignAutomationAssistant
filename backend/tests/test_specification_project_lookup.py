"""Reference resolution is read-only, prefix-aware and independent of holding links."""

from collections import Counter

from fastapi import HTTPException
import pytest

from backend.app import monday_client
from backend.app.monday_client import MondayProjectBoardItem
from backend.app.services.design_processing_target import MondayDesignProcessingReadGateway
from backend.app.services.legacy_enquiry.matching import build_matching_contract, match_projects


def project(identity, reference, *, state="active", title="Existing roof"):
    return MondayProjectBoardItem(str(identity), reference, title, state, "2017-01-01")


def raw_project(identity, reference, *, state="active"):
    return {"id": str(identity), "name": reference, "state": state, "column_values": [
        {"id": "text3__1", "text": "Existing roof"}, {"id": "date9__1", "text": "2017-01-01"},
    ]}


def board_page(items=(), cursor=None):
    return {"data": {"boards": [{"items_page": {"items": list(items), "cursor": cursor}}]}}


def test_direct_query_uses_variables_no_date_cutoff_and_complete_pagination(monkeypatch):
    calls = []
    pages = [board_page([raw_project(1, "CD00123"), raw_project(2, "AB123")], "second"),
        {"data": {"next_items_page": {"items": [raw_project(3, "ab 00123"),
            raw_project(4, "AB00123", state="archived")], "cursor": None}}}]

    def request(token, query, variables=None, **kwargs):
        calls.append((query, variables))
        return pages.pop(0)

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    items = monday_client.fetch_project_items_matching_references("offline", "70001", ["AB00123_26.01 - AA"])
    assert [item.item_id for item in items] == ["3", "4"]
    query, variables = calls[0]
    assert "mutation" not in query.casefold()
    assert "AB00123" not in query and "00123" not in query
    assert variables == {"boardIds": ["70001"], "limit": 500,
        "queryParams": {"operator": "or", "rules": [
            {"column_id": "name", "compare_value": ["00123"], "operator": "contains_text"}]}}
    assert calls[1][1] == {"cursor": "second", "limit": 500}


def test_bare_project_is_resolved_only_with_exact_prefix_child_drawing_confirmation(monkeypatch):
    confirmed = {**raw_project(1, "00123"), "subitems": [
        {"name": "AB00123_26.01 - AA", "state": "active"},
        {"name": "CD00123_26.01 - B", "state": "active"}]}
    unconfirmed = {**raw_project(2, "00123"), "subitems": [
        {"name": "CD00123_26.01 - B", "state": "active"}]}
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs:
        board_page([confirmed, unconfirmed]))
    items = monday_client.fetch_project_items_matching_references("offline", "70001", ["AB00123"])
    assert len(items) == 1
    assert items[0].project_reference == "00123"
    assert items[0].reference_confirmations == ("AB00123_26.01 - AA",)
    result = match_projects("Existing roof", MatchingBoard(exact=items), detected_references=["AB00123"])
    candidate = build_matching_contract("Existing roof", result)["candidates"][0]
    assert candidate["projectReference"] == "00123"
    assert candidate["resolvedProjectReference"] == "AB00123"
    assert candidate["referenceConfirmations"] == ["AB00123_26.01 - AA"]


def test_unreadable_bare_project_drawing_confirmations_are_not_a_valid_empty_result(monkeypatch):
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs:
        board_page([raw_project(1, "00123")]))
    with pytest.raises(monday_client.MondayReadContractError, match="unreadable drawing confirmations"):
        monday_client.fetch_project_items_matching_references("offline", "70001", ["AB00123"])


@pytest.mark.parametrize("page", [{"items": []}, {"items": [], "cursor": ""},
    {"items": [], "cursor": 9}, {"items": None, "cursor": None}])
def test_incomplete_reference_inventory_is_unavailable_not_empty(monkeypatch, page):
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs:
        {"data": {"boards": [{"items_page": page}]}})
    with pytest.raises(monday_client.MondayReadContractError):
        monday_client.fetch_project_items_matching_references("offline", "70001", ["AB00123"])


def test_repeated_reference_cursor_does_not_loop_or_return_partial_result(monkeypatch):
    pages = [board_page([raw_project(1, "AB00123")], "same"),
        {"data": {"next_items_page": {"items": [], "cursor": "same"}}}]
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs: pages.pop(0))
    with pytest.raises(monday_client.MondayReadContractError, match="repeated"):
        monday_client.fetch_project_items_matching_references("offline", "70001", ["AB00123"])


@pytest.mark.parametrize("method", ["fetch_project_items_matching_full_text", "fetch_active_project_items_since"])
def test_title_and_fallback_inventories_are_complete_and_repeated_cursors_fail(monkeypatch, method):
    pages = [board_page([raw_project(1, "AB00123")], "second"),
        {"data": {"next_items_page": {"items": [raw_project(2, "CD00456")], "cursor": None}}}]
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs: pages.pop(0))
    arguments = ["offline", "70001"] + (["100 SE1"] if "full_text" in method else [])
    assert [item.item_id for item in getattr(monday_client, method)(*arguments)] == ["1", "2"]
    pages.extend([board_page([], "same"), {"data": {"next_items_page": {"items": [], "cursor": "same"}}}])
    with pytest.raises(monday_client.MondayReadContractError, match="repeated"):
        getattr(monday_client, method)(*arguments)


@pytest.mark.parametrize("references", [[], ["00123"], ["D.S - 00123_26.01 - A"],
    ['AB00123\") { mutation'], [f"AB{index}" for index in range(33)]])
def test_invalid_or_unbounded_reference_query_does_not_call_monday(monkeypatch, references):
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs: pytest.fail("Unexpected Monday read"))
    with pytest.raises(ValueError):
        monday_client.fetch_project_items_matching_references("offline", "70001", references)


class MatchingBoard:
    def __init__(self, exact=(), title=()):
        self.exact = tuple(exact)
        self.title = tuple(title)
        self.calls = Counter()

    def fetch_project_items_matching_references(self, references):
        self.calls["references"] += 1
        return self.exact

    def fetch_project_word_hit_counts(self, words):
        return {word: 1 for word in words}

    def fetch_project_items_matching_any_words(self, words, *, start_date):
        return self.title

    def fetch_active_project_items_since(self, *, start_date):
        return self.title


def test_exact_and_title_candidates_remain_distinct_and_similarity_is_not_intent():
    board = MatchingBoard(exact=[project(1, "AB00123"), project(2, "AB00123"),
        project(3, "AB00123", state="archived"), project(9, "CD00123")],
        title=[project(1, "AB00123"), project(4, "EF00456")])
    result = match_projects("Existing roof", board, detected_references=[
        {"literal": "AB00123_26.01 - A", "source_id": "request:body"}])
    assert [match["id"] for match in result["matches"]] == ["1", "2", "4"]
    assert len(result["referenceLookup"]["candidates"]) == 3
    assert result["referenceLookup"]["status"] == "available"
    contract = build_matching_contract("Existing roof", result)
    assert contract["schemaVersion"] == 2
    assert contract["candidates"][0]["matchKind"] == "exact_reference"
    assert contract["candidates"][-1]["matchKind"] == "title_similarity"
    assert "recommended_type" not in result
    assert board.calls == {"references": 1}


def test_optional_reference_failure_retains_title_candidates_and_reports_unavailable():
    board = MatchingBoard(title=[project(4, "EF00456")])

    def unavailable(*args, **kwargs):
        raise HTTPException(502, "private upstream context")

    board.fetch_project_items_matching_references = unavailable
    result = match_projects("Existing roof", board, detected_references=["AB00123"])
    assert [match["id"] for match in result["matches"]] == ["4"]
    assert result["referenceLookup"]["status"] == "unavailable"
    assert "private upstream" not in str(result)


def test_empty_reference_result_is_available_and_not_a_lookup_failure():
    result = match_projects("Existing roof", MatchingBoard(), detected_references=["AB00123"])
    assert result["matches"] == []
    assert result["referenceLookup"]["status"] == "available"
    assert result["referenceLookup"]["candidates"] == []


def test_gateway_cache_is_short_lived_typed_and_bound_to_instance(monkeypatch):
    calls = []
    now = [100.0]
    items = (project(1, "AB00123"),)
    monkeypatch.setattr("backend.app.services.design_processing_target.monotonic", lambda: now[0])
    monkeypatch.setattr(monday_client, "fetch_project_items_matching_references",
        lambda token, board, references: calls.append((token, board, references)) or items)
    gateway = MondayDesignProcessingReadGateway("first", "70001")
    assert gateway.fetch_project_items_matching_references(["AB00123"]) == items
    assert gateway.fetch_project_items_matching_references(["ab 00123_26.01 - A"]) == items
    assert len(calls) == 1
    MondayDesignProcessingReadGateway("second", "70001").fetch_project_items_matching_references(["AB00123"])
    now[0] += 31
    gateway.fetch_project_items_matching_references(["AB00123"])
    assert len(calls) == 3


def test_specification_project_cache_returns_detached_data_and_checks_board(monkeypatch):
    from backend.app.services.performance_specification_sources import MondaySpecificationGateway

    calls = []
    item = {"id": "1", "board": {"id": "70001"}, "subitems": [{"id": "2"}]}
    monkeypatch.setattr(MondaySpecificationGateway, "fetch_project", lambda self, identity:
        calls.append(identity) or item)
    gateway = MondayDesignProcessingReadGateway("first", "70001")
    first = gateway.fetch_specification_project("1")
    first["subitems"].clear()
    assert gateway.fetch_specification_project("1")["subitems"] == [{"id": "2"}]
    assert calls == ["1"]
    with pytest.raises(monday_client.MondayReadContractError, match="another board"):
        MondayDesignProcessingReadGateway("first", "70002").fetch_specification_project("1")
