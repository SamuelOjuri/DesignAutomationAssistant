"""Database diagnostics retain failure categories without SQL or credentials."""

import json
from types import SimpleNamespace

import pytest
from google.genai import types
from sqlalchemy.exc import OperationalError, ProgrammingError, TimeoutError as PoolTimeout

from backend.app.config import settings
from backend.app.services.database_diagnostics import database_error_details, sanitized_database_details
from backend.app.services.gemini_requests import GeminiRequestFailure, request_gemini
from backend.app.services.legacy_enquiry.source_documents import IncompleteSourceExtraction, _failure_summary
from backend.app.services import source_transcription_cache as cache


SECRET = "DB_DETAIL_SECRET postgres://user:private-password@private-host/db"


def database_exception(state=None, message=SECRET, error_class=OperationalError):
    original = RuntimeError(message)
    original.sqlstate = state
    return error_class("SELECT PRIVATE_SQL", {"private_parameter": SECRET}, original)


@pytest.mark.parametrize("state,classification,transient", [
    ("53300", "connection_limit", True),
    ("08000", "connection_failure", True), ("08006", "connection_failure", True),
    ("57P01", "server_unavailable", True), ("57P02", "server_unavailable", True), ("57P03", "server_unavailable", True),
    ("40P01", "deadlock", True), ("40001", "serialization_failure", True),
    ("42P01", "missing_relation", False), ("42501", "permission_denied", False),
    ("28P01", "authentication", False), ("28000", "authentication", False),
    ("99999", "operational", False),
])
def test_sqlstate_categories_are_explicit_and_messages_are_never_retained(state, classification, transient):
    details = database_error_details(database_exception(state))
    assert details == {"error_type": "OperationalError", "sqlstate": state,
                       "database_error": classification, "transient": transient}
    assert "PRIVATE_SQL" not in json.dumps(details) and "DB_DETAIL_SECRET" not in json.dumps(details)


@pytest.mark.parametrize("message,classification", [
    ("MaxClientsInSessionMode: max clients reached", "connection_limit"),
    ("Max client connections reached", "connection_limit"),
    ("No more connections allowed (max_client_conn)", "connection_limit"),
    ("remaining connection slots are reserved", "connection_limit"),
    ("connection timed out", "connection_timeout"),
    ("connection refused", "connection_refused"),
    ("could not translate host name", "connection_resolution"),
    ("SSL SYSCALL error: EOF detected", "connection_lost"),
    ("password authentication failed", "authentication"),
    ("permission denied for table", "permission_denied"),
    ("relation private_table does not exist", "missing_relation"),
])
def test_known_driver_messages_produce_only_safe_categories(message, classification):
    details = database_error_details(database_exception(message=message + " " + SECRET))
    assert details["database_error"] == classification
    assert "sqlstate" not in details
    assert "DB_DETAIL_SECRET" not in json.dumps(details)
    assert "private_table" not in json.dumps(details)


def test_pool_timeout_is_retryable_but_generic_operational_error_is_not_guessed():
    assert database_error_details(PoolTimeout(SECRET)) == {
        "error_type": "TimeoutError", "database_error": "pool_timeout", "transient": True,
    }
    assert database_error_details(database_exception())["transient"] is False
    invalidated = database_exception()
    invalidated.connection_invalidated = True
    assert database_error_details(invalidated)["database_error"] == "connection_lost"
    assert database_error_details(RuntimeError("Max client connections reached " + SECRET)) is None


def test_generic_pooler_sqlstate_retains_state_and_recognizes_known_connection_limit():
    details = database_error_details(database_exception("XX000", "Max client connections reached " + SECRET))
    assert details["sqlstate"] == "XX000"
    assert details["database_error"] == "connection_limit" and details["transient"] is True
    specific = database_error_details(database_exception("28P01", "Max client connections reached " + SECRET))
    assert specific["database_error"] == "authentication" and specific["transient"] is False


def test_direct_psycopg_error_and_programming_error_keep_their_actual_classification():
    direct = type("OperationalError", (Exception,), {"__module__": "psycopg.errors"})(SECRET)
    direct.sqlstate = "08006"
    assert database_error_details(direct)["database_error"] == "connection_failure"
    details = database_error_details(database_exception("42P01", error_class=ProgrammingError))
    assert details["error_type"] == "ProgrammingError" and details["transient"] is False


@pytest.mark.parametrize("state", ["53300 " + SECRET, "42p01", "53300\nPASSWORD", "123456", 53300])
def test_invalid_sqlstate_is_not_preserved(state):
    details = database_error_details(database_exception(state))
    assert "sqlstate" not in details and "DB_DETAIL_SECRET" not in json.dumps(details)


def test_wrapper_metadata_is_whitelisted_and_cannot_mark_authentication_transient():
    safe = sanitized_database_details({"database_error": "authentication", "error_type": SECRET,
        "sqlstate": "28P01", "transient": True, "message": SECRET, "sql": SECRET})
    assert safe == {"database_error": "authentication", "error_type": "DatabaseError", "sqlstate": "28P01", "transient": False}
    assert sanitized_database_details({"database_error": SECRET, "error_type": "OperationalError"}) is None
    assert sanitized_database_details({"database_error": ["connection_limit"]}) is None
    assert sanitized_database_details({"database_error": {"message": SECRET}}) is None


def test_admission_database_failure_survives_gemini_and_source_summary_without_secret_leakage():
    class AdmissionFailure(RuntimeError):
        origin = "gemini_admission"
        database_details = {**database_error_details(database_exception("53300")), "message": SECRET, "sql": SECRET}

    def fail(*args):
        raise AdmissionFailure(SECRET)

    with pytest.raises(GeminiRequestFailure) as caught:
        request_gemini(fail, "offline", [], types.GenerateContentConfig(), stage="image_text")
    details = caught.value.details
    assert details["http_status"] is None
    assert details["origin"] == "gemini_admission"
    assert details["error_type"] == "OperationalError"
    assert details["database_error"] == "connection_limit" and details["sqlstate"] == "53300"
    summary = str(IncompleteSourceExtraction([{
        "filename": "logo.png", "status": "failed:GeminiRequestFailure", "error": details,
    }]))
    assert "origin=gemini_admission" in summary
    assert "error_type=OperationalError" in summary and "sqlstate=53300" in summary
    assert "database_error=connection_limit" in summary
    assert "DB_DETAIL_SECRET" not in summary and "PRIVATE_SQL" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def test_direct_database_failure_is_distinguishable_from_provider_http_error():
    def fail(*args):
        raise database_exception("42501", error_class=ProgrammingError)
    with pytest.raises(GeminiRequestFailure) as caught:
        request_gemini(fail, "offline", [], types.GenerateContentConfig(), stage="pdf_text")
    details = caught.value.details
    assert details["origin"] == "database" and details["database_error"] == "permission_denied"
    assert details["error_type"] == "ProgrammingError" and details["transient"] is False


def test_cache_warnings_include_safe_read_and_write_database_metadata(monkeypatch, caplog):
    monkeypatch.setattr(settings, "design_processing_transcription_cache_enabled", True)

    def missing_relation(*args):
        raise database_exception("42P01", error_class=ProgrammingError)

    def connection_limit(*args):
        raise database_exception(message="Max client connections reached " + SECRET)

    monkeypatch.setattr(cache, "read_text", missing_relation)
    monkeypatch.setattr(cache, "save_text", connection_limit)

    @cache.cached_transcription("image")
    def transcribe(client, content, filename):
        return "Literal source text"

    client = SimpleNamespace(model="offline", thinking_level="medium", transcription_cache_scope="scope")
    assert transcribe(client, b"source", "logo.png") == "Literal source text"
    assert '"sqlstate": "42P01"' in caplog.text
    assert '"database_error": "missing_relation"' in caplog.text
    assert '"database_error": "connection_limit"' in caplog.text
    assert "DB_DETAIL_SECRET" not in caplog.text and "PRIVATE_SQL" not in caplog.text


def test_source_summary_does_not_echo_untrusted_diagnostic_values():
    summary = _failure_summary({"filename": "logo.png", "status": "failed:GeminiRequestFailure", "error": {
        "stage": "image_text", "http_status": None, "mode": "text", "error_type": SECRET,
        "database_error": SECRET, "sqlstate": SECRET, "origin": SECRET,
    }})
    assert "DB_DETAIL_SECRET" not in summary
