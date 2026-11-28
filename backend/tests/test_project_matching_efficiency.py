"""Project identity, relevance and request budgets, using an offline board."""

from collections import Counter

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
    def __init__(self, items=()):
        self.items = tuple(items)
        self.calls = Counter()
        self.requests = []

    def fetch_project_title_candidates(self, *, exact_title=None, words=(), match_all=False, budget):
        self.calls["exact" if exact_title is not None else "candidates"] += 1
        self.requests.append({"exact_title": exact_title, "words": tuple(words), "match_all": match_all})
        budget.pages_read += 1
        eligible = [item for item in self.items if item.state == "active"]
        if exact_title is not None:
            items = [item for item in eligible if item.project_title == exact_title]
        else:
            predicate = all if match_all else any
            items = [item for item in eligible if predicate(
                word.casefold() in item.project_title.casefold() for word in words)]
        return monday_client.MondayProjectSearchResult(tuple(items), True)


def test_pomeroy_historical_exact_matches_do_not_expand_to_street_projects():
    board = Board([project(3861, "Pomeroy Street", created_date="2018-10-16"),
                   project(684, "Pomeroy Street", created_date="2015-10-08"),
                   *[project(index, "Ebury Street") for index in range(1, 590)]])
    result = matching.match_projects("Pomeroy Street", board)
    assert {item["id"] for item in result["matches"]} == {"3861", "684"}
    assert all(item["match_kind"] == "exact_title" for item in result["matches"])
    assert board.calls == {"exact": 1}
    assert result["titleLookup"]["pagesRead"] == 1
    assert result["searchDiagnostics"]["totalQualifiedCandidateCount"] == 2


def test_missing_dates_do_not_exclude_exact_identity_and_inactive_rows_are_excluded():
    board = Board([project(1, "Pomeroy Street", created_date=None),
                   project(2, "Pomeroy Street", state="archived"),
                   project(3, "Pomeroy Street", state="deleted")])
    assert [item["id"] for item in matching.match_projects("Pomeroy Street", board)["matches"]] == ["1"]


@pytest.mark.parametrize("project_name,title", [
    ("Pomeroy Street", " POMEROY   STREET "),
    ("Pomeroy Street", "Pomeroy-Street"),
    ("St John's House", "ST JOHN\u2019S HOUSE"),
    ("40-42 Holden Rd", "40 / 42 Holden Rd"),
])
def test_normalized_whole_title_matches_are_retrieved_and_preferred(project_name, title):
    board = Board([project(1, title), project(2, "Pomeroy Road extension")])
    result = matching.match_projects(project_name, board)
    assert [item["id"] for item in result["matches"]] == ["1"]
    assert result["matches"][0]["match_kind"] == "normalized_title"
    assert board.calls == {"exact": 1, "candidates": 1}


def test_title_normalization_preserves_numbers_and_does_not_expand_street_saint():
    assert matching.normalize_project_title("12 Pomeroy Street") != matching.normalize_project_title("14 Pomeroy Street")
    assert matching.normalize_project_title("St John") != matching.normalize_project_title("Saint John")


@pytest.mark.parametrize("threshold", [0.55, 0.99])
def test_generic_word_cannot_admit_a_candidate_regardless_of_character_score(threshold):
    board = Board([project(1, "Ebury Street"), project(2, "Pedro Street, E")])
    result = matching.match_projects("Pomeroy Street", board, similarity_threshold=threshold)
    assert result["matches"] == []
    assert board.requests[1]["words"] == ("pomer",)
    assert board.calls == {"exact": 1, "candidates": 1}


def test_relaxed_matches_require_whole_identifying_words():
    board = Board([project(1, "Pomeroy Road"), project(2, "Pomeroysmith Street"),
                   project(3, "Ebury Street")])
    result = matching.match_projects("Pomeroy Street", board)
    assert [item["id"] for item in result["matches"]] == ["1"]
    assert result["matches"][0]["match_kind"] == "title_similarity"


@pytest.mark.parametrize("name,title", [
    ("Briarwic", "Briarwick"),
    ("Pomeroy Street", "Pomery Road"),
])
def test_small_identifier_spelling_variants_use_the_same_two_search_stages(name, title):
    board = Board([project(1, title), project(2, "Ebury Street"), project(3, "Pomeroysmith Street")])
    result = matching.match_projects(name, board)
    assert [item["id"] for item in result["matches"]] == ["1"]
    assert board.calls == {"exact": 1, "candidates": 1}


def test_address_location_cannot_independently_admit_an_unrelated_project():
    board = Board([project(1, "Pomeroy Street phase 2"), project(2, "Cardiff Hospital")])
    result = matching.match_projects("Pomeroy Street Cardiff", board)
    assert [item["id"] for item in result["matches"]] == ["1"]
    assert board.requests[1]["words"] == ("pomer",)


def test_location_only_results_do_not_qualify_when_the_street_name_is_missing():
    result = matching.match_projects("Pomeroy Street Cardiff", Board([project(1, "Cardiff Hospital")]))
    assert result["matches"] == []


def test_identifying_word_coverage_precedes_character_similarity():
    board = Board([project(1, "Alpha Beta Gamma extremely long development title"),
                   project(2, "Alpha Beta"), project(3, "Beta Gamma")])
    result = matching.match_projects("Alpha Beta Gamma", board)
    assert result["matches"][0]["id"] == "1"
    assert {item["id"] for item in result["matches"]} == {"1", "2", "3"}


@pytest.mark.parametrize("name,title,expected", [
    ("Industrial Estate", "INDUSTRIAL-ESTATE", ["1"]),
    ("Street", "Streetsbrook Infant Academy", []),
    ("100 SE1", "100 other SE1", []),
    ("100 SE1", "100   se1", ["1"]),
])
def test_generic_or_numeric_only_names_require_whole_title_agreement(name, title, expected):
    board = Board([project(1, title)])
    assert [item["id"] for item in matching.match_projects(name, board)["matches"]] == expected
    assert board.requests[1]["match_all"]


@pytest.mark.parametrize("name", ["", "   ", "---"])
def test_empty_or_unsearchable_titles_do_not_call_monday(name):
    board = Board()
    result = matching.match_projects(name, board)
    assert result["matches"] == [] and result["titleLookup"]["status"] == "not_requested"
    assert not board.calls


def test_ranks_all_qualified_candidates_before_display_limit_and_exposes_truncation():
    items = [project(index, f"Kings Road extension {index}") for index in range(1, 501)]
    items.append(project(501, "Kings Road Annex"))
    result = matching.match_projects("Kings Road", Board(items))
    assert len(result["matches"]) == 500 and result["matches"][0]["id"] == "501"
    contract = matching.build_matching_contract("Kings Road", result)
    assert contract["candidateCount"] == 500
    assert contract["searchDiagnostics"]["totalQualifiedCandidateCount"] == 501
    assert contract["searchDiagnostics"]["truncated"] is True


def test_ties_are_deterministic_independent_of_page_order():
    items = [project(2, "Kings Road extension"), project(1, "Kings Road extension")]
    assert matching.match_projects("Kings Road", Board(items))["matches"] == matching.match_projects(
        "Kings Road", Board(reversed(items)))["matches"]


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


def test_normalized_whole_title_can_be_found_after_first_candidate_page(monkeypatch):
    calls = []

    def request(token, query, variables=None, **kwargs):
        calls.append((query, variables))
        if variables and variables.get("queryParams", {}).get("rules", [{}])[0].get("operator") == "any_of":
            return board_page()
        if variables and "queryParams" in variables:
            return board_page([raw_project(index, "Alpha") for index in range(1, 501)], "page-2")
        return {"data": {"next_items_page": {"items": [raw_project(501, "ALPHA-BETA")], "cursor": None}}}

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    result = matching.match_projects("Alpha Beta", MondayDesignProcessingReadGateway("offline", "1825117125"))
    assert [item["id"] for item in result["matches"]] == ["501"]
    assert len(calls) == 3


@pytest.mark.parametrize("has_partial_candidates", [False, True])
def test_shared_page_budget_is_explicitly_incomplete_and_does_not_mean_new(monkeypatch, has_partial_candidates):
    calls = []

    def request(token, query, variables=None, **kwargs):
        calls.append((query, variables, kwargs))
        if len(calls) == 1:
            return board_page()
        items = [raw_project(len(calls), "Pomeroy Road")] if has_partial_candidates else []
        page = {"items": items, "cursor": f"page-{len(calls)}"}
        return ({"data": {"boards": [{"items_page": page}]}} if "queryParams" in variables
                else {"data": {"next_items_page": page}})

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    result = matching.match_projects("Pomeroy Street", MondayDesignProcessingReadGateway("offline", "1825117125"))
    contract = matching.build_matching_contract("Pomeroy Street", result)
    assert len(calls) == 4 and all(call[2]["retry_attempts"] == 1 for call in calls)
    assert contract["titleLookup"]["status"] == "incomplete"
    assert contract["titleLookup"]["pagesRead"] == 4
    assert contract["searchDiagnostics"]["totalQualifiedCandidateCount"] is None
    assert result["type"] == ("existing" if has_partial_candidates else "unknown")
    assert len(contract["candidates"]) == (3 if has_partial_candidates else 0)


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


@pytest.mark.parametrize("phase", ["exact", "candidates"])
def test_upstream_failures_do_not_become_new_projects(phase):
    board = Board()
    error = HTTPException(502, "Monday unavailable")
    original = board.fetch_project_title_candidates

    def fail(*args, **kwargs):
        if (kwargs.get("exact_title") is not None) == (phase == "exact"):
            raise error
        return original(*args, **kwargs)

    board.fetch_project_title_candidates = fail
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
    calls, checks = [], []

    def request(token, query, variables=None, **kwargs):
        calls.append((query, variables))
        return board_page()

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    gateway = _CheckedMatchingGateway(
        MondayDesignProcessingReadGateway("offline", "1825117125"), lambda: checks.append(True),
    )
    result = matching.match_projects(name, gateway)
    assert result["matches"] == [] and result["titleLookup"]["status"] == "available"
    # The pipeline validates before and after matching; quick reads add no re-reads.
    assert (len(calls), len(checks)) == (2, 0)


def test_matching_reads_use_the_extraction_check_cadence_instead_of_rereading_each_call():
    # Item 3391548442: two Monday re-reads around every matching call took minutes.
    now, local, remote, reads = [0.0], [], [], []

    class Gateway:
        def read(self, value):
            reads.append(value)
            now[0] += 2
            return value

    gateway = _CheckedMatchingGateway(
        Gateway(), lambda: remote.append(now[0]), lambda: local.append(now[0]),
        local_validation_seconds=5, remote_validation_seconds=60, clock=lambda: now[0],
    )
    assert [gateway.read(index) for index in range(40)] == list(range(40))
    assert remote == [60]
    assert local == [6, 12, 18, 24, 30, 36, 42, 48, 54, 66, 72, 78]

    def cancelled():
        raise RuntimeError("stored_identity_changed")

    gateway = _CheckedMatchingGateway(Gateway(), lambda: None, cancelled,
                                      local_validation_seconds=5, clock=lambda: now[0])
    now[0] += 5
    with pytest.raises(RuntimeError, match="stored_identity_changed"):
        gateway.read("after cancellation")
    assert "after cancellation" not in reads


def test_long_titles_do_not_expand_the_number_of_queries_or_claim_completeness():
    name = "Ashford Briarwick Cedarwood Dunelm Eastbrook Fairhaven Glenwood Heathside Ivydale Juniperfield Kingswood Larchmont Meadowbrook"
    board = Board()
    result = matching.match_projects(name, board)
    assert board.calls == {"exact": 1, "candidates": 1}
    assert len(board.requests[1]["words"]) == 12
    assert result["titleLookup"]["status"] == "incomplete" and result["type"] == "unknown"
