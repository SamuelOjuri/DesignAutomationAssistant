from __future__ import annotations

from datetime import datetime, timezone
import os
from queue import Queue
import threading
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import (
    CheckConstraint,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend.app.db import Base
from backend.app import models as app_models


PHASE2_MODELS = (
    app_models.DesignProcessingItem,
    app_models.DesignProcessingJob,
    app_models.DesignProcessingArtifact,
    app_models.MondayWebhookDispatch,
)
PHASE2_TABLES = {model.__tablename__ for model in PHASE2_MODELS}


def _create_schema_engine(database_url: str, schema: str):
    engine = create_engine(database_url, pool_pre_ping=True)

    def configure_connection(
        dbapi_connection,
        _connection_record,
        _connection_proxy,
    ) -> None:
        previous_autocommit = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}", public')
        finally:
            dbapi_connection.autocommit = previous_autocommit

    event.listen(engine, "checkout", configure_connection)
    return engine


def _constraint_names(table, constraint_type):
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, constraint_type) and constraint.name is not None
    }


def _assert_orm_matches_migration(connection, *, schema: str) -> None:
    inspector = inspect(connection)
    for table_name in PHASE2_TABLES:
        orm_table = Base.metadata.tables[table_name]
        reflected_columns = {
            column["name"]: column
            for column in inspector.get_columns(table_name, schema=schema)
        }
        assert set(reflected_columns) == {
            column.name for column in orm_table.columns
        }
        for column in orm_table.columns:
            assert reflected_columns[column.name]["nullable"] == column.nullable

        reflected_checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name, schema=schema)
            if constraint["name"] is not None
        }
        assert reflected_checks == _constraint_names(orm_table, CheckConstraint)

        reflected_uniques = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name, schema=schema)
            if constraint["name"] is not None
        }
        assert reflected_uniques == _constraint_names(orm_table, UniqueConstraint)

        reflected_indexes = {
            index["name"]
            for index in inspector.get_indexes(table_name, schema=schema)
            if index.get("duplicates_constraint") is None
        }
        assert reflected_indexes == {index.name for index in orm_table.indexes}

        reflected_foreign_keys = {
            foreign_key["name"]
            for foreign_key in inspector.get_foreign_keys(table_name, schema=schema)
            if foreign_key["name"] is not None
        }
        assert reflected_foreign_keys == {
            foreign_key.name
            for foreign_key in orm_table.foreign_key_constraints
            if foreign_key.name is not None
        }


def _assert_execution_identity_is_immutable(engine) -> None:
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO design_processing_items (
                    board_id, item_id, latest_desired_input_revision,
                    latest_desired_pipeline_version, state
                ) VALUES (
                    '1882196103', 'immutable-item', 'revision-a',
                    'pipeline-v1', 'processing'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO design_processing_jobs (
                    board_id, item_id, trigger_type, execution_kind,
                    execution_input_revision, execution_pipeline_version,
                    status, scheduled_for
                ) VALUES (
                    '1882196103', 'immutable-item', 'test', 'analysis',
                    'revision-a', 'pipeline-v1', 'running', :now
                )
                """
            ),
            {"now": now},
        )

    with pytest.raises(DBAPIError, match="execution identity is immutable"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE design_processing_jobs
                    SET execution_input_revision = 'revision-b'
                    WHERE item_id = 'immutable-item'
                    """
                )
            )


def _assert_concurrent_active_job_inserts_are_serialized(engine) -> None:
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO design_processing_items (board_id, item_id, state)
                VALUES ('1882196103', 'race-item', 'scheduled')
                """
            )
        )

    barrier = threading.Barrier(2)
    outcomes: Queue[BaseException | str] = Queue()

    def insert_active_job(worker_number: int) -> None:
        try:
            with engine.begin() as connection:
                barrier.wait(timeout=5)
                connection.execute(
                    text(
                        """
                        INSERT INTO design_processing_jobs (
                            id, board_id, item_id, trigger_type, status, scheduled_for
                        ) VALUES (
                            :id, '1882196103', 'race-item', :trigger_type,
                            'scheduled', :scheduled_for
                        )
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "trigger_type": f"worker-{worker_number}",
                        "scheduled_for": now,
                    },
                )
            outcomes.put("inserted")
        except IntegrityError:
            outcomes.put("conflict")
        except BaseException as exc:
            outcomes.put(exc)

    threads = [
        threading.Thread(target=insert_active_job, args=(worker_number,), daemon=True)
        for worker_number in (1, 2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    results = [outcomes.get(timeout=1), outcomes.get(timeout=1)]
    unexpected = [result for result in results if isinstance(result, BaseException)]
    if unexpected:
        raise unexpected[0]
    assert sorted(results) == ["conflict", "inserted"]

    with engine.connect() as connection:
        active_count = connection.execute(
            text(
                """
                SELECT count(*)
                FROM design_processing_jobs
                WHERE board_id = '1882196103'
                  AND item_id = 'race-item'
                  AND status IN ('scheduled', 'running', 'retry_wait')
                """
            )
        ).scalar_one()
    assert active_count == 1


def test_phase2_migration_round_trip_and_postgres_invariants(monkeypatch):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL migration tests")

    schema = f"design_processing_phase2_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    test_engine = None
    migrated_to_phase2 = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        monkeypatch.setenv("DATABASE_URL", database_url)
        test_engine = _create_schema_engine(database_url, schema)
        with test_engine.begin() as connection:
            assert (
                connection.execute(text("SELECT current_schema()")).scalar_one()
                == schema
            )
            alembic_config = Config("backend/alembic.ini")
            alembic_config.attributes["connection"] = connection
            alembic_config.attributes["version_table_schema"] = schema
            command.upgrade(alembic_config, "0009_snapshot_lifecycle")
        with test_engine.connect() as connection:
            assert PHASE2_TABLES.isdisjoint(
                inspect(connection).get_table_names(schema=schema)
            )

        with test_engine.begin() as connection:
            alembic_config = Config("backend/alembic.ini")
            alembic_config.attributes["connection"] = connection
            alembic_config.attributes["version_table_schema"] = schema
            command.upgrade(alembic_config, "0010_design_processing_queue")
        migrated_to_phase2 = True
        with test_engine.connect() as connection:
            assert PHASE2_TABLES <= set(
                inspect(connection).get_table_names(schema=schema)
            )
            _assert_orm_matches_migration(connection, schema=schema)
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0010_design_processing_queue"

        _assert_execution_identity_is_immutable(test_engine)
        _assert_concurrent_active_job_inserts_are_serialized(test_engine)

        with test_engine.begin() as connection:
            alembic_config = Config("backend/alembic.ini")
            alembic_config.attributes["connection"] = connection
            alembic_config.attributes["version_table_schema"] = schema
            command.downgrade(alembic_config, "0009_snapshot_lifecycle")
        migrated_to_phase2 = False
        with test_engine.connect() as connection:
            assert PHASE2_TABLES.isdisjoint(
                inspect(connection).get_table_names(schema=schema)
            )
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0009_snapshot_lifecycle"
    finally:
        if migrated_to_phase2 and test_engine is not None:
            with test_engine.begin() as connection:
                cleanup_config = Config("backend/alembic.ini")
                cleanup_config.attributes["connection"] = connection
                cleanup_config.attributes["version_table_schema"] = schema
                command.downgrade(
                    cleanup_config,
                    "0009_snapshot_lifecycle",
                )
        if test_engine is not None:
            test_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()