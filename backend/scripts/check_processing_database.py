"""Read-only checks for processing cache/admission database deployment.

Run with the same DATABASE_URL and runtime role as the affected backend process:
python -m backend.scripts.check_processing_database
No source rows, credentials, model requests, queue changes, or SQL writes occur.
"""

from __future__ import annotations

import argparse
import json
import re

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from backend.app.services.database_diagnostics import database_error_details


REQUIRED_REVISION = "0021_processing_acceleration"
REQUIRED_TABLES = ("source_transcription_cache", "gemini_request_leases")
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


def _error(check, exc):
    detail = database_error_details(exc)
    return {"check": check, **(detail or {
        "database_error": "configuration_error" if check == "connectivity" else "diagnostic_error",
        "transient": False,
    })}


def check_connection(connection):
    """Inspect only schema metadata and role capabilities in a read-only session."""
    report = {"event": "processing_database_diagnostic", "status": "failed",
              "required_revision": REQUIRED_REVISION, "checks": [], "errors": []}
    if connection.dialect.name != "postgresql":
        report["errors"].append({"check": "database_type", "database_error": "postgresql_required",
                                 "transient": False})
        return report
    # This connection is never returned to an application pool. PostgreSQL also
    # enforces read-only transactions if a future query is accidentally changed.
    connection = connection.execution_options(postgresql_readonly=True)
    stage = "connectivity"
    try:
        connected = connection.execute(text("SELECT 1")).scalar_one() == 1
        report["checks"].append({"check": stage, "passed": connected})
        if not connected:
            report["errors"].append({"check": stage, "database_error": "unexpected_database_response",
                                     "transient": False})
            return report
        stage = "migration"
        present = connection.execute(text("SELECT to_regclass(:relation) IS NOT NULL"),
                                     {"relation": "alembic_version"}).scalar_one()
        if present:
            revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            # Revision IDs are code metadata, but still validate before printing:
            # a malformed row must never become a way to echo connection/source data.
            safe_revisions = [value for value in revisions
                              if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,128}", value)]
            migration_ready = REQUIRED_REVISION in safe_revisions and len(safe_revisions) == len(revisions)
            report["checks"].append({"check": stage, "passed": migration_ready,
                                     "revisions": safe_revisions, "revision_metadata_valid": len(safe_revisions) == len(revisions)})
        else:
            migration_ready = False
            report["checks"].append({"check": stage, "passed": False, "revisions": []})
        if not migration_ready:
            report["errors"].append({"check": stage, "database_error": "migration_required",
                                     "transient": False})
        for table_name in REQUIRED_TABLES:
            stage = table_name
            exists = bool(connection.execute(text("SELECT to_regclass(:relation) IS NOT NULL"),
                                             {"relation": table_name}).scalar_one())
            check = {"check": "table_access", "table": table_name, "exists": exists, "passed": False}
            report["checks"].append(check)
            if not exists:
                report["errors"].append({"check": stage, "database_error": "missing_relation",
                                         "transient": False})
                continue
            permissions = connection.execute(text("""
                SELECT has_table_privilege(current_user, to_regclass(:relation), 'SELECT') AS can_select,
                       has_table_privilege(current_user, to_regclass(:relation), 'INSERT') AS can_insert,
                       has_table_privilege(current_user, to_regclass(:relation), 'UPDATE') AS can_update,
                       has_table_privilege(current_user, to_regclass(:relation), 'DELETE') AS can_delete,
                       row_security_active(to_regclass(:relation)) AS row_security_active,
                       c.relowner = r.oid AS is_owner, r.rolsuper AS is_superuser, r.rolbypassrls AS bypass_rls
                FROM pg_class c CROSS JOIN pg_roles r
                WHERE c.oid = to_regclass(:relation) AND r.rolname = current_user
            """), {"relation": table_name}).mappings().one()
            check.update(privileges={name: bool(permissions["can_" + name.lower()]) for name in PRIVILEGES},
                         row_security_active=permissions["row_security_active"],
                         is_owner=bool(permissions["is_owner"]), is_superuser=bool(permissions["is_superuser"]),
                         bypass_rls=bool(permissions["bypass_rls"]))
            privileges_ready = all(check["privileges"].values())
            # 0021 intentionally creates no browser-role policies. Active RLS
            # would hide cache/lease rows and break shared admission correctness.
            rls_ready = check["row_security_active"] is False
            check["passed"] = privileges_ready and rls_ready
            if not privileges_ready:
                report["errors"].append({"check": stage, "database_error": "permission_denied",
                                         "transient": False})
            if not rls_ready:
                report["errors"].append({"check": stage, "database_error": "row_security_blocks_runtime",
                                         "transient": False})
    except Exception as exc:
        report["errors"].append(_error(stage, exc))
    if not report["errors"]:
        report["status"] = "ok"
    return report


def check_database(configuration, *, engine_factory=create_engine):
    """Open one short-lived connection, without creating a worker/client/session."""
    engine = None
    try:
        options = {"poolclass": NullPool, "hide_parameters": True, "echo": False}
        if configuration.database_url.startswith("postgresql"):
            options["connect_args"] = {"connect_timeout": getattr(configuration, "database_connect_timeout_seconds", 10)}
        engine = engine_factory(configuration.database_url, **options)
        with engine.connect() as connection:
            return check_connection(connection)
    except Exception as exc:
        return {"event": "processing_database_diagnostic", "status": "failed",
                "required_revision": REQUIRED_REVISION, "checks": [], "errors": [_error("connectivity", exc)]}
    finally:
        if engine is not None:
            engine.dispose()


def main(argv=None, *, configuration=None, engine_factory=create_engine):
    class PrivateArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            # Unknown arguments might contain a mistakenly supplied URL/token.
            self.print_usage()
            self.exit(2, "Unsupported diagnostic arguments. Use --help.\n")

    parser = PrivateArgumentParser(description=__doc__)
    parser.parse_args(argv)
    # --help works without loading credentials or attempting a connection.
    try:
        if configuration is None:
            from backend.app.config import settings
            configuration = settings
        report = check_database(configuration, engine_factory=engine_factory)
    except Exception:
        # Settings validation can contain environment values; never print it.
        report = {"event": "processing_database_diagnostic", "status": "failed", "checks": [],
                  "errors": [{"check": "configuration", "database_error": "configuration_error", "transient": False}]}
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
