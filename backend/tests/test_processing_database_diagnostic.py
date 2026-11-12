"""Database deployment checks use only synthetic connections and SQLite memory."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from backend.scripts import check_processing_database as diagnostic


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def mappings(self):
        return self

    def one(self):
        return self.value


class Connection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, *, missing=(), revision=diagnostic.REQUIRED_REVISION, denied=(), active_rls=(), failure=None):
        self.missing = set(missing)
        self.revision = revision
        self.denied = set(denied)
        self.active_rls = set(active_rls)
        self.failure = failure
        self.statements = []
        self.options = {}

    def execution_options(self, **options):
        self.options.update(options)
        return self

    def execute(self, statement, parameters=None):
        sql = str(statement).strip()
        self.statements.append((sql, parameters))
        assert sql.upper().startswith("SELECT "), "Diagnostic attempted a SQL write"
        if self.failure:
            raise self.failure
        if sql == "SELECT 1":
            return Result(1)
        if sql.startswith("SELECT to_regclass"):
            return Result(parameters["relation"] not in self.missing)
        if sql == "SELECT version_num FROM alembic_version":
            return Result([self.revision])
        if "has_table_privilege" in sql:
            table = parameters["relation"]
            return Result({**{"can_" + permission.lower(): (table, permission) not in self.denied
                              for permission in diagnostic.PRIVILEGES},
                           "row_security_active": table in self.active_rls,
                           "is_owner": False, "is_superuser": False, "bypass_rls": table not in self.active_rls})
        raise AssertionError("Unexpected diagnostic query")


class Engine:
    def __init__(self, connection=None, failure=None):
        self.connection = connection
        self.failure = failure
        self.disposed = False

    def connect(self):
        if self.failure:
            raise self.failure
        return self

    def __enter__(self):
        return self.connection

    def __exit__(self, *args):
        pass

    def dispose(self):
        self.disposed = True


def test_healthy_database_checks_schema_privileges_and_rls_without_reading_source_rows():
    connection = Connection()
    report = diagnostic.check_connection(connection)
    assert report["status"] == "ok" and not report["errors"]
    assert connection.options == {"postgresql_readonly": True}
    checks = [check for check in report["checks"] if check["check"] == "table_access"]
    assert [check["table"] for check in checks] == list(diagnostic.REQUIRED_TABLES)
    assert all(check["passed"] and not check["row_security_active"] for check in checks)
    assert all(all(check["privileges"].values()) for check in checks)
    assert not any("SELECT *" in sql or "SELECT text" in sql for sql, _ in connection.statements)


def test_missing_migration_and_tables_have_actionable_distinct_results():
    report = diagnostic.check_connection(Connection(missing=("alembic_version", *diagnostic.REQUIRED_TABLES)))
    assert report["status"] == "failed"
    assert [(error["check"], error["database_error"]) for error in report["errors"]] == [
        ("migration", "migration_required"),
        ("source_transcription_cache", "missing_relation"),
        ("gemini_request_leases", "missing_relation"),
    ]


def test_older_revision_is_not_mistaken_for_completed_migration():
    report = diagnostic.check_connection(Connection(revision="0020_monday_item_availability"))
    assert report["status"] == "failed"
    assert report["errors"] == [{"check": "migration", "database_error": "migration_required", "transient": False}]


def test_grants_and_active_rls_are_checked_separately_without_attempting_writes():
    report = diagnostic.check_connection(Connection(denied={("source_transcription_cache", "INSERT")},
                                                    active_rls={"gemini_request_leases"}))
    assert report["status"] == "failed"
    assert report["errors"] == [
        {"check": "source_transcription_cache", "database_error": "permission_denied", "transient": False},
        {"check": "gemini_request_leases", "database_error": "row_security_blocks_runtime", "transient": False},
    ]


def database_failure(sqlstate):
    cause = Exception("PRIVATE host user password postgresql://person:secret@private-host/private-db")
    cause.pgcode = sqlstate
    return OperationalError("PRIVATE SQL", {"PRIVATE": "parameter"}, cause)


def test_connectivity_limit_is_sanitized_and_cli_exits_nonzero(capsys):
    engine = Engine(failure=database_failure("53300"))
    configuration = SimpleNamespace(database_url="postgresql://private-user:secret@private-host/private-db")
    code = diagnostic.main([], configuration=configuration, engine_factory=lambda *args, **kwargs: engine)
    output = capsys.readouterr().out
    report = json.loads(output)
    assert code == 1 and engine.disposed
    assert report["errors"][0]["database_error"] == "connection_limit"
    assert report["errors"][0]["sqlstate"] == "53300"
    assert not any(value in output for value in ("PRIVATE", "private-host", "private-user", "secret", "postgresql://"))


def test_permission_exception_is_sanitized_without_original_sql_or_message():
    report = diagnostic.check_connection(Connection(failure=database_failure("42501")))
    assert report["errors"][0]["database_error"] == "permission_denied"
    assert "PRIVATE" not in json.dumps(report)


def test_malformed_revision_metadata_is_not_echoed():
    report = diagnostic.check_connection(Connection(revision="postgresql://private-user:secret@private-host/private-db"))
    assert report["status"] == "failed"
    migration = next(check for check in report["checks"] if check["check"] == "migration")
    assert migration["revisions"] == [] and not migration["revision_metadata_valid"]
    assert "private" not in json.dumps(report)


def test_sqlite_is_explicitly_unsupported_and_does_not_run_postgresql_sql():
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        report = diagnostic.check_connection(connection)
    engine.dispose()
    assert report["status"] == "failed"
    assert report["errors"][0]["database_error"] == "postgresql_required"


def test_cli_help_does_not_open_a_database(capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("--help attempted a database connection")
    with pytest.raises(SystemExit) as caught:
        diagnostic.main(["--help"], engine_factory=unexpected)
    assert caught.value.code == 0
    assert "Read-only" in capsys.readouterr().out


def test_cli_invalid_arguments_do_not_echo_mistaken_credentials(capsys):
    with pytest.raises(SystemExit) as caught:
        diagnostic.main(["postgresql://private-user:secret@private-host/private-db"])
    captured = capsys.readouterr()
    assert caught.value.code == 2
    assert not any(value in captured.out + captured.err for value in ("private-user", "secret", "private-host", "postgresql://"))


def test_success_uses_one_nonpooled_connection_and_never_prints_its_url(capsys):
    connection = Connection()
    engine = Engine(connection)
    options = {}
    configuration = SimpleNamespace(database_url="postgresql://private-user:secret@private-host/private-db",
                                    database_connect_timeout_seconds=10)
    def factory(url, **kwargs):
        options.update(kwargs)
        return engine
    assert diagnostic.main([], configuration=configuration, engine_factory=factory) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "ok" and engine.disposed
    assert options["poolclass"] is diagnostic.NullPool
    assert options["connect_args"] == {"connect_timeout": 10} and options["hide_parameters"]
    assert not any(value in output for value in ("private-host", "private-user", "secret", "postgresql://"))
