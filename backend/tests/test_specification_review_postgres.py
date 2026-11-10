"""Real PostgreSQL migration and database-boundary checks for review persistence.

Set TEST_DATABASE_URL to a disposable PostgreSQL database. Every test creates and
drops a UUID-named schema, never migrates the database's public schema.
"""

from __future__ import annotations

import os
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, event, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend.app.db import Base
from backend.app.models import (
    PerformanceSpecificationApproval,
    PerformanceSpecificationReview,
    PerformanceSpecificationReviewEvent,
)


REVISION = "0019_specification_review"
PREVIOUS = "0018_specification_evidence"
TABLES = {model.__tablename__ for model in (
    PerformanceSpecificationReview, PerformanceSpecificationReviewEvent,
    PerformanceSpecificationApproval,
)}


def _migrate(connection, schema, revision, *, downgrade=False):
    config = Config("backend/alembic.ini")
    config.attributes.update(connection=connection, version_table_schema=schema)
    (command.downgrade if downgrade else command.upgrade)(config, revision)


@pytest.fixture()
def postgres_migration_database(monkeypatch):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL review migration tests")
    schema = f"spec_review_migration_{uuid.uuid4().hex}"
    admin = create_engine(database_url, pool_pre_ping=True)
    engine = create_engine(database_url, pool_pre_ping=True)

    @event.listens_for(engine, "checkout")
    def isolate(connection, _record, _proxy):
        previous = connection.autocommit
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}", public')
                cursor.execute("SET statement_timeout TO 10000")
                cursor.execute("SET lock_timeout TO 3000")
        finally:
            connection.autocommit = previous

    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        monkeypatch.setenv("DATABASE_URL", database_url)
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            _migrate(connection, schema, PREVIOUS)
        yield engine, schema
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def _seed_legacy(connection, item="123"):
    connection.execute(text("""
        INSERT INTO design_processing_items (board_id, item_id, state)
        VALUES ('1882196103', :item, 'ready_for_review')
    """), {"item": item})
    connection.execute(text("""
        INSERT INTO performance_specifications (board_id, item_id)
        VALUES ('1882196103', :item)
    """), {"item": item})
    connection.execute(text("""
        INSERT INTO performance_specification_artifacts (
            board_id, item_id, revision, translation_json, source_json,
            model_version, filename, storage_bucket, storage_object_key,
            content_sha256, size_bytes, status, monday_asset_id
        ) VALUES ('1882196103', :item, 'historic-pdf', '{}', :source,
            'legacy-model', 'retained.pdf', 'private', 'retained.pdf',
            :digest, 100, 'published', 'asset-123')
    """), {"item": item, "digest": "a" * 64, "source": '{"legacy":true}'})


def _seed_review(connection, item="123", account="200"):
    connection.execute(text("""
        INSERT INTO performance_specification_reviews (
            board_id, item_id, monday_account_id, source_fingerprint,
            source_identity_json, decision_digest
        ) VALUES ('1882196103', :item, :account, 'source-1', '{}', :digest)
    """), {"item": item, "account": account, "digest": "b" * 64})


def _insert_event(connection, *, item="123", account="200", key="save-1"):
    return connection.scalar(text("""
        INSERT INTO performance_specification_review_events (
            monday_account_id, board_id, item_id, actor_app_user_id,
            actor_monday_user_id, command_type, prior_review_version,
            resulting_review_version, original_decisions_json,
            replacement_decisions_json, base_source_fingerprint,
            idempotency_key, request_digest, result_json
        ) VALUES (:account, '1882196103', :item, 'app-reviewer', '100',
            'save', 0, 1, '{}', '{}', 'source-1', :key, :digest, '{}') RETURNING id
    """), {"item": item, "account": account, "key": key, "digest": "c" * 64})


def _insert_approval(connection, *, item="123", account="200"):
    return connection.scalar(text("""
        INSERT INTO performance_specification_approvals (
            monday_account_id, board_id, item_id, artifact_revision,
            review_version, source_fingerprint, extraction_identity,
            decision_digest, actor_app_user_id, actor_monday_user_id
        ) VALUES (:account, '1882196103', :item, 'historic-pdf',
            1, 'source-1', 'extraction-1', :digest, 'app-reviewer', '100') RETURNING id
    """), {"item": item, "account": account, "digest": "b" * 64})


def _assert_schema_parity(connection, schema):
    inspector = inspect(connection)
    for name in TABLES:
        model = Base.metadata.tables[name]
        reflected = {column["name"]: column for column in inspector.get_columns(name, schema=schema)}
        assert set(reflected) == set(model.columns.keys())
        assert all(reflected[column.name]["nullable"] == column.nullable for column in model.columns)
        for kind, getter in (
            (CheckConstraint, inspector.get_check_constraints),
            (UniqueConstraint, inspector.get_unique_constraints),
        ):
            assert {value["name"] for value in getter(name, schema=schema)} == {
                constraint.name for constraint in model.constraints if isinstance(constraint, kind)
            }
        assert {value["name"] for value in inspector.get_foreign_keys(name, schema=schema)} == {
            constraint.name for constraint in model.foreign_key_constraints
        }
        assert {value["name"] for value in inspector.get_indexes(name, schema=schema)
                if not value.get("duplicates_constraint")} == {index.name for index in model.indexes}


def test_review_migration_round_trip_retains_historic_pdf(postgres_migration_database):
    engine, schema = postgres_migration_database
    with engine.begin() as connection:
        _seed_legacy(connection)
        _migrate(connection, schema, REVISION)
    with engine.connect() as connection:
        _assert_schema_parity(connection, schema)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        assert connection.scalar(text("SELECT count(*) FROM performance_specification_reviews")) == 0
        assert connection.scalar(text("SELECT count(*) FROM performance_specification_approvals")) == 0
        assert connection.scalar(text("SELECT source_json FROM performance_specification_artifacts")) == {"legacy": True}
    with engine.begin() as connection:
        _seed_review(connection)
        _insert_event(connection)
        _insert_approval(connection)
        _migrate(connection, schema, PREVIOUS, downgrade=True)
    with engine.connect() as connection:
        assert TABLES.isdisjoint(inspect(connection).get_table_names(schema=schema))
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == PREVIOUS
        assert connection.scalar(text("SELECT monday_asset_id FROM performance_specification_artifacts")) == "asset-123"
        assert connection.scalar(text("SELECT source_json FROM performance_specification_artifacts")) == {"legacy": True}
    with engine.begin() as connection:
        _migrate(connection, schema, REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM performance_specification_approvals")) == 0


def test_review_database_enforces_history_tenant_and_idempotency(postgres_migration_database):
    engine, schema = postgres_migration_database
    with engine.begin() as connection:
        _migrate(connection, schema, REVISION)
        for item in ("123", "456"):
            _seed_legacy(connection, item)
            _seed_review(connection, item)
        event_id = _insert_event(connection)
        approval_id = _insert_approval(connection)
    for table, record_id in (
        ("performance_specification_review_events", event_id),
        ("performance_specification_approvals", approval_id),
    ):
        for statement in (f"UPDATE {table} SET item_id = item_id WHERE id = :id", f"DELETE FROM {table} WHERE id = :id"):
            with pytest.raises(DBAPIError, match="history is immutable"):
                with engine.begin() as connection:
                    connection.execute(text(statement), {"id": record_id})
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_event(connection)  # Same actor/item/idempotency key.
    for insert in (_insert_event, _insert_approval):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                insert(connection, account="999")
    for statement in (
        "UPDATE performance_specification_artifacts SET source_json = '{}'",
        "UPDATE performance_specification_artifacts SET translation_json = '{\"new\": true}'",
        "UPDATE performance_specification_artifacts SET content_sha256 = 'changed'",
        "UPDATE performance_specification_artifacts SET filename = 'replacement.pdf'",
    ):
        with pytest.raises(DBAPIError, match="immutable"):
            with engine.begin() as connection:
                connection.execute(text(statement))
    with engine.begin() as connection:
        connection.execute(text("""
            UPDATE performance_specification_artifacts
            SET status = 'published', monday_asset_id = 'recovered-asset', last_error = NULL
        """))  # Upload recovery may still amend publication state.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("""
                UPDATE performance_specification_reviews SET current_approval_id = :id
                WHERE board_id = '1882196103' AND item_id = '456'
            """), {"id": approval_id})
    with engine.begin() as connection:
        connection.execute(text("""
            UPDATE performance_specification_reviews SET current_approval_id = :id
            WHERE board_id = '1882196103' AND item_id = '123'
        """), {"id": approval_id})
        connection.execute(text("UPDATE performance_specification_reviews SET current_approval_id = NULL"))
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM performance_specification_approvals")) == 1
