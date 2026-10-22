"""Matching parity and request budgets, using an offline Monday board."""

from collections import Counter
import itertools
import random

from fastapi import HTTPException
import pytest

from backend.app import monday_client
from backend.app.monday_client import MondayProjectBoardItem
from backend.app.services.design_processing_pipeline import _CheckedMatchingGateway
from backend.app.services.design_processing_target import MondayDesignProcessingReadGateway
from backend.app.services.legacy_enquiry import matching


def project(item_id, title, *, state="active", created_date="2025-02-10"):
    return MondayProjectBoardItem(str(item_id), str(item_id), title, state, created_date)


class Board:
    def __init__(self, items=(), *, counts=None):
        self.items = tuple(items)
        self.counts = counts
        self.calls = Counter()

    def eligible(self, start_date):
        return tuple(item for item in self.items if (
            item.state == "active" and item.created_date and item.created_date >= start_date
        ))

    def fetch_project_word_hit_counts(self, words):
        self.calls["counts"] += 1
        if self.counts is not None:
            return {word: self.counts[word] for word in words}
        return {word: min(50, sum(
            item.state == "active" and word.lower() in item.project_title.lower()
            for item in self.items
        )) for word in words}

    def fetch_project_items_matching_words(self, words, *, start_date):
        self.calls["subsets"] += 1
        return tuple(item for item in self.eligible(start_date) if all(
            word.lower() in item.project_title.lower() for word in words
        ))[:500]

    def fetch_project_items_matching_any_words(self, words, *, start_date):
        self.calls["candidates"] += 1
        return tuple(item for item in self.eligible(start_date) if any(
            word.lower() in item.project_title.lower() for word in words
        ))

    def fetch_project_items_matching_full_text(self, name, *, start_date):
        self.calls["phrase"] += 1
        return tuple(item for item in self.eligible(start_date)
                     if name.lower() in item.project_title.lower())[:500]

    def fetch_active_project_items_since(self, *, start_date):
        self.calls["fallback"] += 1
        return self.eligible(start_date)


def legacy_match(name, gateway, *, similarity_threshold=0.55):
    """The prior remote-subset policy, retained only as an independent oracle."""
    words = matching.filter_meaningful_words(name)
    if not words:
        return matching._result_with_matches(matching._ordered_matches(
            gateway.fetch_project_items_matching_full_text(
                name, start_date=matching.PROJECT_SEARCH_START_DATE,
            ), name,
        ))
    counts = gateway.fetch_project_word_hit_counts(words)
    ordered = sorted(words, key=lambda word: counts.get(word, 1_000_000))
    for size in range(len(ordered), 0, -1):
        for subset in itertools.combinations(ordered, size):
            matches = matching._ordered_matches(
                gateway.fetch_project_items_matching_words(
                    subset, start_date=matching.PROJECT_SEARCH_START_DATE,
                ), name,
            )
            if matches:
                return matching._result_with_matches(matches)
    return matching._result_with_matches(matching._ordered_matches(
        gateway.fetch_active_project_items_since(start_date=matching.PROJECT_SEARCH_START_DATE),
        name, similarity_threshold=similarity_threshold,
    ))


@pytest.mark.parametrize("seed", range(40))
def test_candidate_selection_matches_legacy_combinations(seed):
    rng = random.Random(seed)
    words = ["Ashford", "Briarwick", "Cedarwood", "Dunelm", "Eastbrook"]
    items = [project(
        index + 1,
        " ".join(rng.sample(words, rng.randrange(1, 6))) + f" building {index}",
        state="archived" if index % 9 == 0 else "active",
        created_date="2020-12-31" if index % 7 == 0 else "2025-02-10",
    ) for index in range(25)]
    counts = {word: rng.randrange(51) for word in words}
    name = " ".join(words)
    board = Board(items, counts=counts)
    assert matching.match_projects(name, board) == legacy_match(name, Board(items, counts=counts))
    assert board.calls == {"counts": 1, "candidates": 1}


@pytest.mark.parametrize("name, titles, counts, expected", [
    ("Alpha Beta Gamma", ["Alpha Beta", "Beta Gamma"],
     {"Alpha": 50, "Beta": 1, "Gamma": 2}, ["2"]),
    ("Kings Kings Road", ["Road", "Kings"], {"Kings": 3, "Road": 1}, ["2"]),
    ("KINGs Road", ["kings-road", "KINGS ROAD extension", "Road"],
     {"KINGs": 1, "Road": 1}, ["1", "2"]),
    ("Ashford Heath", ["AshfordHeath", "Heath"], {"Ashford": 1, "Heath": 2}, ["1"]),
])
def test_specificity_ties_repeated_words_case_and_substrings(name, titles, counts, expected):
    items = [project(index + 1, title) for index, title in enumerate(titles)]
    result = matching.match_projects(name, Board(items, counts=counts))
    assert result == legacy_match(name, Board(items, counts=counts))
    assert [item["id"] for item in result["matches"]] == expected


@pytest.mark.parametrize("threshold", [0.55, 0.99])
def test_empty_candidate_pool_preserves_fuzzy_fallback(threshold):
    items = [project(1, "Briarwic"), project(2, "Briarwick", state="deleted"),
             project(3, "Briarwick", created_date="2020-01-01")]
    board = Board(items)
    result = matching.match_projects("Briarwick", board, similarity_threshold=threshold)
    assert result == legacy_match("Briarwick", Board(items), similarity_threshold=threshold)
    assert result["exists"] == (threshold == 0.55)
    assert board.calls == {"counts": 1, "candidates": 1, "fallback": 1}


def test_no_meaningful_words_preserves_phrase_path():
    items = [project(1, "100 SE1"), project(2, "100 other SE1")]
    board = Board(items)
    assert matching.match_projects("100 SE1", board) == legacy_match("100 SE1", Board(items))
    assert board.calls == {"phrase": 1}


def test_preserves_first_500_matching_items_before_similarity_sort():
    items = [project(index, "Kings Road extension") for index in range(1, 501)]
    items.append(project(501, "Kings Road"))
    result = matching.match_projects("Kings Road", Board(items))
    assert result == legacy_match("Kings Road", Board(items))
    assert len(result["matches"]) == 500
    assert "501" not in {item["id"] for item in result["matches"]}


def raw_project(item_id, title="Kings Road", *, state="active"):
    return {"id": str(item_id), "name": str(item_id), "state": state, "column_values": [
        {"id": "text3__1", "text": title}, {"id": "date9__1", "text": "2025-02-10"},
    ]}


def board_page(items=(), cursor=None):
    return {"data": {"boards": [{"items_page": {"items": list(items), "cursor": cursor}}]}}


def test_candidate_query_keeps_date_outside_word_or_and_paginates(monkeypatch):
    calls = []
    pending = [board_page([raw_project(1), raw_project(2, state="archived")], "page-2"),
               {"data": {"next_items_page": {"items": [raw_project(1), raw_project(3)], "cursor": None}}}]

    def request(token, query, variables=None, **kwargs):
        calls.append((query, variables))
        return pending.pop(0)

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    words = ["Kings", 'Road"', "Kings"]
    result = monday_client.fetch_project_items_matching_any_words("offline", "1825117125", words)
    params = calls[0][1]["queryParams"]
    assert params == {
        "operator": "and",
        "rules": [{"column_id": "date9__1", "compare_value": ["EXACT", "2021-01-01"],
                   "operator": "greater_than_or_equals"}],
        "groups": [{"operator": "or", "rules": [
            {"column_id": "text3__1", "compare_value": [word], "operator": "contains_text"}
            for word in words[:2]
        ]}],
    }
    assert calls[0][1]["boardIds"] == ["1825117125"]
    assert 'Road"' not in calls[0][0]  # Search terms are GraphQL variables.
    assert calls[1][1] == {"cursor": "page-2", "limit": 500}
    assert "query_params" not in calls[1][0]
    assert [item.item_id for item in result] == ["1", "3"]


def test_winning_combination_can_be_found_after_first_candidate_page(monkeypatch):
    calls = []

    def request(token, query, variables=None, **kwargs):
        calls.append((query, variables))
        if "w0: boards" in query:
            return {"data": {"w0": [{"items_page": {"items": [{}] * 50}}],
                             "w1": [{"items_page": {"items": [{}]}}]}}
        if variables and "queryParams" in variables:
            return board_page([raw_project(index, "Alpha") for index in range(1, 501)], "page-2")
        return {"data": {"next_items_page": {"items": [raw_project(501, "Alpha Beta")], "cursor": None}}}

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    result = matching.match_projects("Alpha Beta", MondayDesignProcessingReadGateway("offline", "1825117125"))
    assert [item["id"] for item in result["matches"]] == ["501"]
    assert len(calls) == 3


@pytest.mark.parametrize("page", [
    {"items": []}, {"items": [], "cursor": ""}, {"items": [], "cursor": 12},
    {"items": None, "cursor": None}, {"items": [None], "cursor": None},
])
def test_incomplete_candidates_are_errors_not_no_matches(monkeypatch, page):
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs:
                        {"data": {"boards": [{"items_page": page}]}})
    with pytest.raises(monday_client.MondayReadContractError):
        monday_client.fetch_project_items_matching_any_words("offline", "1825117125", ["Kings"])


def test_repeated_cursor_stops_without_looping(monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return board_page([], "repeated")
        return {"data": {"next_items_page": {"items": [], "cursor": "repeated"}}}

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    with pytest.raises(monday_client.MondayReadContractError, match="repeated"):
        monday_client.fetch_project_items_matching_any_words("offline", "1825117125", ["Kings"])
    assert len(calls) == 2


@pytest.mark.parametrize("phase", ["counts", "candidates", "fallback"])
def test_upstream_failures_do_not_become_new_projects(phase):
    board = Board()
    method = {"counts": "fetch_project_word_hit_counts", "candidates": "fetch_project_items_matching_any_words",
              "fallback": "fetch_active_project_items_since"}[phase]
    error = HTTPException(502, "Monday unavailable")

    def fail(*args, **kwargs):
        raise error

    setattr(board, method, fail)
    with pytest.raises(HTTPException) as raised:
        matching.match_projects("Kings Road", board)
    assert raised.value is error


def test_continuation_failure_does_not_return_partial_candidates(monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return board_page([raw_project(1)], "next")
        raise monday_client.TransientMondayAPIError(503)

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    with pytest.raises(monday_client.TransientMondayAPIError):
        monday_client.fetch_project_items_matching_any_words("offline", "1825117125", ["Kings"])


@pytest.mark.parametrize("words, board_id, start_date, limit", [
    ([], "123", "2021-01-01", 500), ([" "], "123", "2021-01-01", 500),
    (["Kings"], "bad", "2021-01-01", 500), (["Kings"], "123", "bad", 500),
    (["Kings"], "123", "2021-01-01", 0), (["Kings"], "123", "2021-01-01", 501),
])
def test_invalid_candidate_queries_never_call_monday(monkeypatch, words, board_id, start_date, limit):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid query called Monday")

    monkeypatch.setattr(monday_client, "monday_graphql_request", unexpected)
    with pytest.raises((ValueError, monday_client.MondayReadContractError)):
        monday_client.fetch_project_items_matching_any_words(
            "offline", board_id, words, start_date=start_date, limit=limit,
        )


@pytest.mark.parametrize("word_count", [6, 8, 10, 12])
def test_no_match_requests_stay_constant_as_word_count_grows(monkeypatch, word_count):
    words = "Ashford Briarwick Cedarwood Dunelm Eastbrook Fairhaven Glenwood Heathside Ivydale Juniperfield Kingswood Larchmont".split()[:word_count]
    name = " ".join(words)
    calls, checks, legacy_checks = [], [], []

    def request(token, query, variables=None, **kwargs):
        calls.append((query, variables))
        if "w0: boards" in query:
            return {"data": {f"w{index}": [{"items_page": {"items": []}}]
                             for index in range(word_count)}}
        return board_page()

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    gateway = _CheckedMatchingGateway(
        MondayDesignProcessingReadGateway("offline", "1825117125"), lambda: checks.append(True),
    )
    legacy_board = Board()
    old_result = legacy_match(name, _CheckedMatchingGateway(legacy_board, lambda: legacy_checks.append(True)))
    assert matching.match_projects(name, gateway) == old_result
    old_requests = sum(legacy_board.calls.values()) + len(legacy_checks)
    new_requests = len(calls) + len(checks)
    assert old_requests == 3 * (2 ** word_count + 1)
    assert (len(calls), len(checks), new_requests) == (3, 6, 9)
    print(f"words={word_count} legacy_requests={old_requests} candidate_requests={new_requests}")
