"""Real row ownership and migration checks in disposable isolated schemas only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session

from backend.app.models import AutoSyncReconciliationCheck, DesignProcessingReconciliationCheck, MondayItemAvailability
from backend.app.services import monday_item_availability as availability


NOW = datetime(2026, 11, 10, 16, tzinfo=timezone.utc)
BOARD, ITEM, TOKEN = "1882196103", "123", "offline-processing-token"


@pytest.fixture
def postgres_database():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for isolated PostgreSQL availability tests")
    schema = f"monday_availability_{uuid.uuid4().hex}"
    admin = create_engine(database_url, pool_pre_ping=True)
    engine = create_engine(database_url, pool_pre_ping=True)

    @event.listens_for(engine, "checkout")
    def isolate(connection, _record, _proxy):
        prior = connection.autocommit
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}", public')
                cursor.execute("SET statement_timeout TO 10000")
                cursor.execute("SET lock_timeout TO 5000")
        finally:
            connection.autocommit = prior

    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            for model in (MondayItemAvailability, AutoSyncReconciliationCheck, DesignProcessingReconciliationCheck):
                model.__table__.create(connection)
        yield engine, schema
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_competing_workers_claim_only_one_shared_probe(postgres_database):
    engine, _ = postgres_database
    start = Barrier(2)

    def claim():
        with Session(engine) as db:
            start.wait(timeout=5)
            token = availability.claim_due_probe(db, BOARD, ITEM, TOKEN, NOW)
            db.commit()
            return token

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: claim(), range(2)))
    assert len([token for token in results if token]) == 1
    with Session(engine) as db:
        row = db.query(MondayItemAvailability).one()
        assert row.probe_token in results


def test_competing_missing_observations_serialize_and_obey_start_time(postgres_database):
    engine, _ = postgres_database
    with Session(engine) as db:
        availability.observe_unavailable(db, BOARD, ITEM, TOKEN, NOW)
        db.commit()
    start = Barrier(2)

    def observe(delay):
        with Session(engine) as db:
            start.wait(timeout=5)
            availability.observe_unavailable(db, BOARD, ITEM, TOKEN, NOW + timedelta(seconds=delay))
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(observe, [2, 1]))
    with Session(engine) as db:
        row = db.query(MondayItemAvailability).one()
        assert row.last_attempted_at == NOW + timedelta(seconds=2)
        assert row.consecutive_misses in {2, 3}
        assert row.state == "unavailable" and row.retired_at is None


def test_additive_migration_round_trip_on_real_postgres(postgres_database, monkeypatch):
    engine, schema = postgres_database
    with engine.begin() as connection:
        MondayItemAvailability.__table__.drop(connection)
        # The new table is deliberately independent of task/specification FKs.
        config = Config("backend/alembic.ini")
        config.attributes.update(connection=connection, version_table_schema=schema)
        monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
        command.stamp(config, "0019_specification_review")
        command.upgrade(config, "0020_monday_item_availability")
        metadata = inspect(connection)
        assert "monday_item_availability" in metadata.get_table_names(schema=schema)
        assert metadata.get_foreign_keys("monday_item_availability", schema=schema) == []
        command.downgrade(config, "0019_specification_review")
        assert "monday_item_availability" not in inspect(connection).get_table_names(schema=schema)
