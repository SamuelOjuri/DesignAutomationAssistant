from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import logging

import pytest
import requests
from fastapi import HTTPException

from backend.app import monday_client


def response(status=200, payload=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload if payload is not None else {"data": {"ok": True}}).encode()
    result.headers.update(headers or {})
    return result


def graphql_error(code, **extensions):
    return {"message": "Upstream message", "extensions": {"code": code, **extensions}}


@pytest.fixture
def client(monkeypatch):
    pending, calls, sleeps = [], [], []

    def post(*args, **kwargs):
        calls.append(kwargs)
        result = pending.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(monday_client.requests, "post", post)
    monkeypatch.setattr(
        monday_client, "_post_monday_graphql",
        monday_client._post_monday_graphql.retry_with(sleep=sleeps.append),
    )
    return pending, calls, sleeps


def test_429_waits_for_longest_server_delay_then_recovers(client, caplog):
    pending, calls, sleeps = client
    pending.extend([
        response(429, {
            "errors": [graphql_error("RATE_LIMIT_EXCEEDED", retry_in_seconds=35)],
            "extensions": {"request_id": "request-429"},
        }, {"Retry-After": "20"}),
        response(),
    ])

    with caplog.at_level(logging.WARNING):
        assert monday_client.monday_graphql_request("secret", "query { ok }") == {"data": {"ok": True}}

    assert len(calls) == 2
    assert sleeps == [35]
    assert "request-429" in caplog.text and "wait_seconds=35" in caplog.text
    record = next(record for record in caplog.records if getattr(record, "event", None) == "monday.api_error")
    assert record.upstream_status_code == 429
    assert record.error_codes == ["RATE_LIMIT_EXCEEDED"]


def test_retry_after_http_date_is_respected(client, monkeypatch):
    pending, _, sleeps = client
    now = datetime(2026, 9, 25, 12, 18, tzinfo=timezone.utc)

    class Clock:
        @staticmethod
        def now(tz):
            return now

    monkeypatch.setattr(monday_client, "datetime", Clock)
    pending.extend([
        response(429, {}, {"Retry-After": format_datetime(now + timedelta(seconds=45), usegmt=True)}),
        response(),
    ])
    monday_client.monday_graphql_request("token", "query { ok }")
    assert sleeps == [45]


@pytest.mark.parametrize("code", [
    "COMPLEXITY_BUDGET_EXHAUSTED", "maxConcurrencyExceeded", "IP_RATE_LIMIT_EXCEEDED",
    "API_TEMPORARILY_BLOCKED", "Minute limit rate exceeded", "INTERNAL_SERVER_ERROR",
])
def test_transient_graphql_errors_retry_inside_request_loop(client, code):
    pending, calls, sleeps = client
    pending.extend([
        response(payload={"data": {"items": None}, "errors": [graphql_error(code, retry_in_seconds=7)]}),
        response(payload={"data": {"items": [{"id": "123"}]}}),
    ])

    item = monday_client.fetch_current_source_revision_inputs("token", "123", account_id="acct")

    assert item == {"id": "123", "account_id": "acct"}
    assert len(calls) == 2 and sleeps == [7]


@pytest.mark.parametrize("payload", [
    {"error_code": "ComplexityException", "error_data": {"retry_in_seconds": 31}},
    {"errors": [graphql_error("ComplexityException", error_data={"retry_in_seconds": 31})]},
    {"errors": [graphql_error("COMPLEXITY_BUDGET_EXHAUSTED")], "retry_in_seconds": 31},
    {"errors": [{"extensions": {"status_code": 429}}], "extensions": {"retry_in_seconds": 31}},
])
def test_graphql_delay_locations_and_legacy_errors(client, payload):
    pending, calls, sleeps = client
    pending.extend([response(payload=payload), response()])
    assert monday_client.monday_graphql_request("token", "# read\n { ok }") == {"data": {"ok": True}}
    assert len(calls) == 2 and sleeps == [31]


@pytest.mark.parametrize("payload", [
    {"errors": [graphql_error("InvalidArgumentException")]},
    {"errors": [graphql_error("UserUnauthorizedException", retry_in_seconds=10)]},
    {"errors": [graphql_error("ComplexityException")]},
    {"errors": [graphql_error("DAILY_LIMIT_EXCEEDED", retry_in_seconds=60)]},
    {"errors": [graphql_error("UNKNOWN_ERROR")]},
    {"errors": [graphql_error("RATE_LIMIT_EXCEEDED"), graphql_error("InvalidColumnIdException")]},
    {"errors": ["Unexpected error"]},
])
def test_permanent_unknown_and_mixed_graphql_errors_are_not_retried(client, payload):
    pending, calls, sleeps = client
    pending.append(response(payload=payload))
    with pytest.raises(HTTPException) as exc_info:
        monday_client.monday_graphql_request("token", "query { ok }")
    assert not isinstance(exc_info.value, monday_client.TransientMondayAPIError)
    assert len(calls) == 1 and sleeps == []


def test_partial_mutation_is_not_replayed_on_graphql_error(client):
    pending, calls, sleeps = client
    pending.append(response(payload={
        "data": {"create_item": {"id": "already-created"}},
        "errors": [graphql_error("INTERNAL_SERVER_ERROR")],
    }))
    with pytest.raises(HTTPException) as exc_info:
        monday_client.monday_graphql_request("token", "# write\n mutation { create_item { id } }")
    assert not isinstance(exc_info.value, monday_client.TransientMondayAPIError)
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("http_status", [200, 429, 503])
def test_retry_exhaustion_retains_diagnostics_and_failure(client, http_status):
    pending, calls, sleeps = client
    pending.extend(response(http_status, {
        "errors": [graphql_error("RATE_LIMIT_EXCEEDED", retry_in_seconds=10)],
        "extensions": {"request_id": "request-last"},
    }) for _ in range(4))
    with pytest.raises(monday_client.TransientMondayAPIError) as exc_info:
        monday_client.monday_graphql_request("token", "query { ok }")

    error = exc_info.value
    assert error.status_code == 502 and error.upstream_status_code == http_status
    assert "RATE_LIMIT_EXCEEDED" in error.detail and "request-last" in error.detail
    assert error.retry_after_seconds == 10
    assert len(calls) == 4 and sleeps == [10, 10, 10]


@pytest.mark.parametrize("delay", ["", "invalid", "-1", "NaN", "Infinity", True, {}, 10**400])
def test_invalid_delays_fall_back_to_exponential_backoff(client, delay):
    pending, calls, sleeps = client
    pending.extend([
        response(429, {"retry_in_seconds": delay}, {"Retry-After": str(delay)}),
        response(),
    ])
    monday_client.monday_graphql_request("token", "query { ok }")
    assert len(calls) == 2 and sleeps == [2]


def test_long_server_delay_surfaces_failure_without_retrying_early(client):
    pending, calls, sleeps = client
    pending.append(response(429, {}, {"Retry-After": "3600"}))
    with pytest.raises(monday_client.TransientMondayAPIError) as exc_info:
        monday_client.monday_graphql_request("token", "query { ok }")
    assert exc_info.value.retry_after_seconds == 3600
    assert "retry_after_seconds=3600" in exc_info.value.detail
    assert len(calls) == 1 and sleeps == []


def test_non_json_http_error_still_retries(client):
    pending, calls, sleeps = client
    error = response(503, headers={"Retry-After": "9", "X-Request-ID": "gateway-request"})
    error._content = b"<html>upstream unavailable</html>"
    pending.extend([error, response()])
    monday_client.monday_graphql_request("token", "query { ok }")
    assert len(calls) == 2 and sleeps == [9]


def test_diagnostics_do_not_log_credentials_payloads_or_upstream_messages(client, caplog):
    pending, _, _ = client
    token = "private-access-token"
    pending.append(response(payload={
        "data": {"name": "confidential-project"},
        "errors": [{
            "message": f"Authorization: {token}\ncustomer@example.com confidential-project",
            "extensions": {"code": "InvalidArgumentException", "error_data": {"token": token}},
        }],
        "extensions": {"request_id": "diagnostic-request"},
    }))
    with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as exc_info:
        monday_client.monday_graphql_request(token, "query { private_field }", {"name": "confidential-project"})

    diagnostics = caplog.text + str(exc_info.value.detail)
    assert "InvalidArgumentException" in diagnostics and "diagnostic-request" in diagnostics
    for sensitive in (token, "confidential-project", "customer@example.com", "private_field", "Authorization"):
        assert sensitive not in diagnostics


@pytest.mark.parametrize("status,allow_unauthorized", [(401, True), (401, False), (403, False)])
def test_authentication_failures_keep_existing_behavior(client, status, allow_unauthorized):
    pending, calls, sleeps = client
    pending.append(response(status, {}))
    if allow_unauthorized:
        assert monday_client.monday_graphql_request("token", "query { me { id } }", allow_unauthorized=True) is None
    else:
        with pytest.raises(HTTPException) as exc_info:
            monday_client.monday_graphql_request("token", "query { me { id } }")
        assert exc_info.value.status_code == (403 if status == 401 else 502)
    assert len(calls) == 1 and sleeps == []
