"""Transient DB recovery never bypasses shared generation admission."""
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from backend.app.config import settings
from backend.app.services import shared_gemini_budget as budget


def database_failure(sqlstate, error_class=OperationalError):
    original = RuntimeError("PRIVATE postgres://user:password@host/db SELECT private_data")
    original.pgcode = sqlstate
    return error_class("SELECT PRIVATE", {}, original)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "gemini_shared_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "gemini_shared_lease_seconds", 900)
    monkeypatch.setattr(settings, "gemini_request_timeout_seconds", 240)


def test_transient_admission_errors_retry_then_release_a_real_admission(enabled, monkeypatch, caplog):
    calls, sleeps, releases = [], [], []
    def acquire(scope):
        calls.append(scope)
        if len(calls) < 3:
            raise database_failure("53300")
        return "admitted-token"
    monkeypatch.setattr(budget, "try_acquire", acquire)
    monkeypatch.setattr(budget, "release", releases.append)
    monkeypatch.setattr(budget.time, "sleep", sleeps.append)
    with budget.shared_generation_slot():
        assert len(calls) == 3
    assert sleeps == [0.5, 1.0] and releases == ["admitted-token"]
    assert "53300" in caplog.text and "PRIVATE" not in caplog.text and "password" not in caplog.text


def test_database_unavailable_after_bounded_retries_never_runs_provider(enabled, monkeypatch):
    calls, sleeps = [], []
    def acquire(scope):
        calls.append(scope)
        raise database_failure("08006")
    monkeypatch.setattr(budget, "try_acquire", acquire)
    monkeypatch.setattr(budget.time, "sleep", sleeps.append)
    with pytest.raises(budget.GeminiAdmissionDatabaseError) as caught:
        with budget.shared_generation_slot():
            pytest.fail("Provider must not run without an admission token")
    assert len(calls) == 3 and sleeps == [0.5, 1.0]
    assert caught.value.origin == "gemini_admission"
    assert caught.value.database_details["sqlstate"] == "08006"
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("sqlstate", ["42P01", "42501", "28P01"])
def test_migration_permissions_and_authentication_fail_without_pointless_retry(enabled, monkeypatch, sqlstate):
    calls = []
    def acquire(scope):
        calls.append(scope)
        raise database_failure(sqlstate, ProgrammingError)
    monkeypatch.setattr(budget, "try_acquire", acquire)
    with pytest.raises(budget.GeminiAdmissionDatabaseError) as caught:
        with budget.shared_generation_slot():
            pytest.fail("Provider must not run")
    assert len(calls) == 1
    assert caught.value.database_details["transient"] is False


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        assert seconds >= 0
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(budget.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(budget.time, "sleep", sleep)
    monkeypatch.setattr(settings, "gemini_shared_wait_seconds", 1)
    return clock


def test_busy_wait_does_not_make_another_database_call_at_the_deadline(enabled, clock, monkeypatch):
    calls, releases = [], []

    def acquire(scope):
        calls.append(clock.now)
        return None

    monkeypatch.setattr(budget, "try_acquire", acquire)
    monkeypatch.setattr(budget, "release", releases.append)
    with pytest.raises(TimeoutError):
        with budget.shared_generation_slot():
            pytest.fail("A busy admission deadline must not start a provider request")
    assert calls == [0.0, 0.5]
    assert clock.sleeps == [0.5, 0.5] and clock.now == 1.0
    assert releases == []


def test_database_retry_backoff_stops_without_starting_an_attempt_at_the_deadline(enabled, clock, monkeypatch):
    calls, releases = [], []

    def acquire(scope):
        calls.append(clock.now)
        raise database_failure("53300")

    monkeypatch.setattr(budget, "try_acquire", acquire)
    monkeypatch.setattr(budget, "release", releases.append)
    with pytest.raises((TimeoutError, budget.GeminiAdmissionDatabaseError)):
        with budget.shared_generation_slot():
            pytest.fail("Transient database retries cannot run the provider after the wait deadline")
    assert calls == [0.0, 0.5]
    assert clock.sleeps == [0.5, 0.5] and clock.now == 1.0
    assert releases == []


def test_token_returned_by_a_slow_database_call_is_released_without_starting_provider(enabled, clock, monkeypatch):
    calls, releases = [], []

    def acquire(scope):
        calls.append(clock.now)
        clock.now += 2.0
        return "late-admission-token"

    monkeypatch.setattr(budget, "try_acquire", acquire)
    monkeypatch.setattr(budget, "release", releases.append)
    with pytest.raises(TimeoutError):
        with budget.shared_generation_slot():
            pytest.fail("A token acquired after the deadline must never start a provider request")
    assert calls == [0.0] and clock.sleeps == []
    assert releases == ["late-admission-token"]
