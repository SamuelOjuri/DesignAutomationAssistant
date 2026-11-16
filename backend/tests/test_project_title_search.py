"""Bounded, all-history Project Name queries and their transport cost."""

import json

import pytest
import requests

from backend.app import monday_client
from backend.app.services.design_processing_target import MondayDesignProcessingReadGateway


def raw_project(identity, title="Pomeroy Street", *, state="active", dated=True):
    return {"id": str(identity), "name": str(identity), "state": state, "column_values": [
        {"id": "text3__1", "text": title},
        *([{"id": "date9__1", "text": "2015-10-08"}] if dated else []),
    ]}


def first_page(items=(), cursor=None):
    return {"data": {"boards": [{"items_page": {"items": list(items), "cursor": cursor}}]}}


def next_page(items=(), cursor=None):
    return {"data": {"next_items_page": {"items": list(items), "cursor": cursor}}}


def search(*, budget=None, **kwargs):
    return monday_client.fetch_project_title_candidates("offline", "1825117125",
        budget=budget or monday_client.MondayProjectSearchBudget(), **kwargs)


def test_exact_title_uses_text_column_variables_without_cutoff_and_one_attempt(monkeypatch):
    calls = []

    def request(token, query, variables=None, **kwargs):
        calls.append((query, variables, kwargs))
        return first_page([raw_project(1), raw_project(2, dated=False),
                           raw_project(3, state="archived")])

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    result = search(exact_title=' Pomeroy "Street" ')
    query, variables, kwargs = calls[0]
    assert result.complete and [item.item_id for item in result.items] == ["1", "2"]
    assert result.items[0].created_date == "2015-10-08" and result.items[1].created_date is None
    assert variables == {"boardIds": ["1825117125"], "limit": 500,
        "queryParams": {"operator": "or", "rules": [{"column_id": "text3__1",
            "compare_value": ['Pomeroy "Street"'], "operator": "any_of"}]}}
    assert 'Pomeroy "Street"' not in query
    assert "greater_than" not in query and "2021" not in query
    assert kwargs["retry_attempts"] == 1 and 0 < kwargs["timeout"] <= 20
    assert len(calls) == 1


@pytest.mark.parametrize("match_all, expected", [(False, "or"), (True, "and")])
def test_selective_word_rules_are_batched_and_deduplicated(monkeypatch, match_all, expected):
    calls = []
    monkeypatch.setattr(monday_client, "monday_graphql_request",
        lambda token, query, variables=None, **kwargs: calls.append(variables) or first_page())
    result = search(words=[" Pomeroy ", "Street", "Pomeroy"], match_all=match_all)
    assert result.complete and result.items == ()
    assert calls[0]["queryParams"] == {"operator": expected, "rules": [
        {"column_id": "text3__1", "compare_value": [word], "operator": "contains_text"}
        for word in ("Pomeroy", "Street")]}
    assert len(calls) == 1


def test_pages_are_deduplicated_and_cursor_requests_keep_original_filter(monkeypatch):
    calls = []
    pages = [first_page([raw_project(1)], "second"),
             next_page([raw_project(1), raw_project(2)], None)]

    def request(token, query, variables=None, **kwargs):
        calls.append((query, variables))
        return pages.pop(0)

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    budget = monday_client.MondayProjectSearchBudget()
    result = search(words=["Pomeroy"], budget=budget)
    assert result.complete and [item.item_id for item in result.items] == ["1", "2"]
    assert budget.pages_read == 2
    assert calls[1][1] == {"cursor": "second", "limit": 500}
    assert "query_params" not in calls[1][0]


def test_budget_is_shared_between_exact_and_selective_stages(monkeypatch):
    calls = []
    pages = [first_page(), first_page([raw_project(1)], "more")]
    monkeypatch.setattr(monday_client, "monday_graphql_request",
        lambda *args, **kwargs: calls.append(args) or pages.pop(0))
    budget = monday_client.MondayProjectSearchBudget(max_pages=2)
    exact = search(exact_title="Pomeroy Street", budget=budget)
    relaxed = search(words=["Pomeroy"], budget=budget)
    further = search(words=["Street"], budget=budget)
    assert exact.complete and exact.items == ()
    assert not relaxed.complete and relaxed.reason == "page_budget_exhausted"
    assert [item.item_id for item in relaxed.items] == ["1"]
    assert not further.complete and further.items == ()
    assert further.reason == "page_budget_exhausted"
    assert budget.pages_read == len(calls) == 2


def test_time_budget_exhaustion_returns_partial_result_without_another_request(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(monday_client, "monotonic", lambda: now[0])
    budget = monday_client.MondayProjectSearchBudget(max_seconds=5)

    def request(*args, **kwargs):
        calls.append(kwargs)
        now[0] += 6
        return first_page([raw_project(1)], "more")

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    result = search(words=["Pomeroy"], budget=budget)
    assert not result.complete and result.reason == "time_budget_exhausted"
    assert [item.item_id for item in result.items] == ["1"]
    assert len(calls) == 1 and calls[0]["timeout"] == 5


def test_expired_budget_is_incomplete_even_when_no_rows_were_read(monkeypatch):
    monkeypatch.setattr(monday_client, "monotonic", lambda: 100.0)
    monkeypatch.setattr(monday_client, "monday_graphql_request",
        lambda *args, **kwargs: pytest.fail("Expired lookup sent a request"))
    result = search(exact_title="Pomeroy Street",
        budget=monday_client.MondayProjectSearchBudget(started_at=0.0))
    assert result.items == () and not result.complete
    assert result.reason == "time_budget_exhausted"


def test_next_page_timeout_uses_remaining_allowance(monkeypatch):
    now = [100.0]
    timeouts = []
    monkeypatch.setattr(monday_client, "monotonic", lambda: now[0])
    budget = monday_client.MondayProjectSearchBudget(max_seconds=25)

    def request(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        now[0] += 10
        return first_page(cursor="second") if len(timeouts) == 1 else next_page()

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    assert search(words=["Pomeroy"], budget=budget).complete
    assert timeouts == [20, 15]


@pytest.mark.parametrize("page", [
    {"items": []}, {"items": [], "cursor": ""}, {"items": [], "cursor": 4},
    {"items": None, "cursor": None}, {"items": [None], "cursor": None},
    {"items": [{**raw_project(1), "column_values": []}], "cursor": None},
])
def test_unreadable_page_is_failure_instead_of_empty_or_partial_success(monkeypatch, page):
    monkeypatch.setattr(monday_client, "monday_graphql_request",
        lambda *args, **kwargs: {"data": {"boards": [{"items_page": page}]}})
    with pytest.raises(monday_client.MondayReadContractError):
        search(words=["Pomeroy"])


def test_repeated_cursor_is_failure_instead_of_budget_exhaustion(monkeypatch):
    pages = [first_page([raw_project(1)], "same"), next_page([raw_project(2)], "same")]
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs: pages.pop(0))
    with pytest.raises(monday_client.MondayReadContractError, match="repeated"):
        search(words=["Pomeroy"])


def test_continuation_failure_does_not_become_completed_lookup(monkeypatch):
    pages = [first_page([raw_project(1)], "second")]

    def request(*args, **kwargs):
        if pages:
            return pages.pop(0)
        raise monday_client.TransientMondayAPIError(503)

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    with pytest.raises(monday_client.TransientMondayAPIError):
        search(words=["Pomeroy"])


@pytest.mark.parametrize("arguments", [
    {}, {"exact_title": ""}, {"exact_title": " "}, {"exact_title": 1},
    {"exact_title": "Pomeroy", "words": ["Street"]}, {"words": [""]},
    {"words": "Pomeroy"}, {"words": [None]}, {"words": [str(i) for i in range(13)]},
    {"words": ["Pomeroy"], "match_all": "and"},
])
def test_invalid_queries_do_not_call_monday(monkeypatch, arguments):
    monkeypatch.setattr(monday_client, "monday_graphql_request",
        lambda *args, **kwargs: pytest.fail("Invalid query sent a request"))
    with pytest.raises(ValueError):
        search(**arguments)


@pytest.mark.parametrize("arguments", [
    {"max_pages": 0}, {"max_pages": 21}, {"max_pages": True},
    {"max_seconds": 0}, {"max_seconds": float("nan")}, {"max_seconds": 121},
    {"pages_read": -1}, {"pages_read": 5}, {"started_at": float("inf")},
])
def test_invalid_budgets_are_rejected(arguments):
    with pytest.raises(ValueError):
        monday_client.MondayProjectSearchBudget(**arguments)


def test_gateway_passes_same_shared_budget_and_bound_credentials(monkeypatch):
    calls = []
    budget = monday_client.MondayProjectSearchBudget()
    result = monday_client.MondayProjectSearchResult((), True)
    monkeypatch.setattr(monday_client, "fetch_project_title_candidates",
        lambda *args, **kwargs: calls.append((args, kwargs)) or result)
    gateway = MondayDesignProcessingReadGateway("first-token", "1825117125")
    assert gateway.fetch_project_title_candidates(words=["Pomeroy"], budget=budget) is result
    assert calls == [(("first-token", "1825117125"), {
        "exact_title": None, "words": ["Pomeroy"], "match_all": False, "budget": budget})]


def test_bounded_read_defers_failure_after_one_http_attempt_without_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(monday_client, "_monday_cooldowns", {})

    def post(*args, **kwargs):
        calls.append(kwargs)
        result = requests.Response()
        result.status_code = 503
        result._content = json.dumps({"errors": [{"message": "Temporary",
            "extensions": {"code": "INTERNAL_SERVER_ERROR", "retry_in_seconds": 5}}]}).encode()
        return result

    monkeypatch.setattr(monday_client.requests, "post", post)
    with pytest.raises(monday_client.TransientMondayAPIError):
        search(exact_title="Pomeroy Street")
    assert len(calls) == 1


@pytest.mark.parametrize("attempts", [0, 5, True, 1.5])
def test_transport_rejects_invalid_attempt_budget_before_http(monkeypatch, attempts):
    monkeypatch.setattr(monday_client.requests, "post", lambda *args, **kwargs: pytest.fail("Unexpected HTTP"))
    with pytest.raises(ValueError):
        monday_client.monday_graphql_request("offline", "query { me { id } }", retry_attempts=attempts)
