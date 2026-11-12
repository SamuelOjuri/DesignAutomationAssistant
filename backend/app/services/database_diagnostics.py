"""Bounded database failure metadata; exception messages never leave this module."""

import re

from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout


TRANSIENT_DATABASE_ERRORS = frozenset({
    "connection_limit", "connection_failure", "server_unavailable", "deadlock",
    "serialization_failure", "pool_timeout", "connection_timeout",
    "connection_refused", "connection_resolution", "connection_lost",
})
DATABASE_ERRORS = TRANSIENT_DATABASE_ERRORS | {
    "missing_relation", "permission_denied", "authentication", "connection_security",
    "operational", "programming", "database_error",
}


def sanitized_database_details(value):
    """Keep only application-owned classification values and bounded identifiers."""
    if not isinstance(value, dict):
        return None
    classification = value.get("database_error")
    if not isinstance(classification, str) or classification not in DATABASE_ERRORS:
        return None
    error_type = value.get("error_type")
    if not isinstance(error_type, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error_type):
        error_type = "DatabaseError"
    result = {"error_type": error_type, "database_error": classification,
              "transient": classification in TRANSIENT_DATABASE_ERRORS}
    state = value.get("sqlstate")
    if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
        result["sqlstate"] = state
    return result


def database_error_details(exc):
    """Classify SQLAlchemy/psycopg failures without retaining SQL, URLs or messages.

    Only known connection patterns supplement SQLSTATE. An unidentified
    OperationalError is not assumed transient, and provider/network exceptions
    outside a recognized database driver remain outside this classification.
    """
    if isinstance(exc, PoolTimeout):
        return sanitized_database_details({"error_type": type(exc).__name__, "database_error": "pool_timeout"})
    driver_error = type(exc).__module__.split(".", 1)[0] in {"psycopg", "psycopg2"}
    if not isinstance(exc, DBAPIError) and not driver_error:
        return None
    original = exc.orig if isinstance(exc, DBAPIError) else exc
    state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    state = state if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state) else None
    classification = None
    if state == "53300":
        classification = "connection_limit"
    elif state and state.startswith("08"):
        classification = "connection_failure"
    elif state in {"57P01", "57P02", "57P03"}:
        classification = "server_unavailable"
    elif state == "40P01":
        classification = "deadlock"
    elif state == "40001":
        classification = "serialization_failure"
    elif state == "42P01":
        classification = "missing_relation"
    elif state == "42501":
        classification = "permission_denied"
    elif state and state.startswith("28"):
        classification = "authentication"

    # Poolers sometimes use PostgreSQL's generic internal-error state for their
    # own connection-limit diagnostics. Preserve that state while recognizing
    # only the same bounded message categories; specific states above win.
    if classification is None and state in {None, "XX000"}:
        try:
            message = str(original).lower()
        except Exception:
            message = ""
        if any(part in message for part in ("password authentication failed", "authentication failed", "invalid password", "no pg_hba.conf entry")):
            classification = "authentication"
        elif "permission denied" in message:
            classification = "permission_denied"
        elif "relation" in message and "does not exist" in message:
            classification = "missing_relation"
        elif any(part in message for part in (
            "maxclientsinsessionmode", "max clients reached", "max client connections reached",
            "no more connections allowed", "too many clients", "too many connections",
            "remaining connection slots are reserved",
        )):
            classification = "connection_limit"
        elif "deadlock detected" in message:
            classification = "deadlock"
        elif "could not serialize access" in message or "serialization failure" in message:
            classification = "serialization_failure"
        elif any(part in message for part in ("database system is starting up", "database system is shutting down", "database system is in recovery mode")):
            classification = "server_unavailable"
        elif "certificate verify failed" in message:
            classification = "connection_security"
        elif any(part in message for part in ("connection timed out", "connection timeout", "timeout expired")):
            classification = "connection_timeout"
        elif "connection refused" in message:
            classification = "connection_refused"
        elif any(part in message for part in ("could not translate host name", "name or service not known", "temporary failure in name resolution", "nodename nor servname")):
            classification = "connection_resolution"
        elif getattr(exc, "connection_invalidated", False) or any(part in message for part in (
            "server closed the connection", "connection already closed", "connection is closed",
            "ssl connection has been closed", "connection reset by peer", "connection terminated unexpectedly",
            "ssl syscall error: eof detected", "network is unreachable",
        )):
            classification = "connection_lost"
    if classification is None:
        name = type(exc).__name__
        classification = "operational" if name == "OperationalError" else "programming" if name == "ProgrammingError" else "database_error"
    details = {"error_type": type(exc).__name__, "database_error": classification}
    if state:
        details["sqlstate"] = state
    return sanitized_database_details(details)
