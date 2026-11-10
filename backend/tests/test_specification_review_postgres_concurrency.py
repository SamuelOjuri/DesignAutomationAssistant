"""Real PostgreSQL guarantees for review CAS, receipts and source-refresh races.

Set TEST_DATABASE_URL to a disposable PostgreSQL database with pgvector
available. Each module run migrates an isolated schema; no live Monday, model
or storage service is used.
"""

from __future__ import annotations

import os
from pathlib import Path
from queue import Queue
import threading
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from backend.app.models import (
    DesignProcessingItem, PerformanceSpecification, PerformanceSpecificationReview,
    PerformanceSpecificationReviewEvent,
)
from backend.app.services import specification_review as service
from backend.tests.test_specification_review_persistence import (
    ACCOUNT, ACTOR, BOARD, save_command, source_snapshot,
)


@pytest.fixture(scope="module")
def postgres_review_database():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL review concurrency tests")
    # PostgreSQL truncates identifiers past 63 bytes. Keep the full UUID so
    # current_schema() matches exactly and concurrent runs remain isolated.
    schema = f"spec_review_concurrency_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    test_engine = None
    previous_database_url = os.environ.get("DATABASE_URL")
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        test_engine = create_engine(database_url, pool_pre_ping=True)

        @event.listens_for(test_engine, "checkout")
        def select_schema(connection, _record, _proxy):
            previous_autocommit = connection.autocommit
            connection.autocommit = True
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f'SET search_path TO "{schema}", public')
                    cursor.execute("SET statement_timeout TO 8000")
                    cursor.execute("SET lock_timeout TO 5000")
            finally:
                connection.autocommit = previous_autocommit

        os.environ["DATABASE_URL"] = database_url
        with test_engine.begin() as connection:
            assert connection.execute(text("SELECT current_schema()")).scalar_one() == schema
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.attributes["connection"] = connection
            config.attributes["version_table_schema"] = schema
            command.upgrade(config, "head")
        yield test_engine, sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    finally:
        if test_engine is not None:
            test_engine.dispose()
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture()
def postgres_review(postgres_review_database):
    _, Session = postgres_review_database
    item_id = str(uuid.uuid4().int)
    with Session() as db:
        db.add(DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id=item_id, state="ready_for_review"))
        db.flush()
        db.add(PerformanceSpecification(board_id=BOARD, item_id=item_id,
            requested_generation=0, completed_generation=0))
        db.commit()
        service.initialize_review(db, board_id=BOARD, item_id=item_id,
            account_id=ACCOUNT, source=source_snapshot())
    return Session, item_id


def race(Session, functions):
    """Start independent connections together and report every thread outcome."""
    barrier = threading.Barrier(len(functions))
    outcomes = Queue()

    def execute(function):
        with Session() as db:
            try:
                barrier.wait(timeout=5)
                outcomes.put(function(db))
            except BaseException as exc:
                outcomes.put(exc)

    threads = [threading.Thread(target=execute, args=(function,), daemon=True) for function in functions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "A competing review transaction did not finish"
    return [outcomes.get(timeout=1) for _ in functions]


def snapshot(Session, item_id):
    with Session() as db:
        review = db.get(PerformanceSpecificationReview, (BOARD, item_id))
        specification = db.get(PerformanceSpecification, (BOARD, item_id))
        return {
            "version": review.review_version,
            "decisions": review.field_decisions_json,
            "source_fingerprint": review.source_fingerprint,
            "generation": specification.requested_generation,
            "save_events": db.query(PerformanceSpecificationReviewEvent).filter_by(
                board_id=BOARD, item_id=item_id, command_type="save_decisions").count(),
            "source_events": db.query(PerformanceSpecificationReviewEvent).filter_by(
                board_id=BOARD, item_id=item_id, command_type="source_changed").count(),
        }


def test_competing_reviewers_get_one_save_and_one_stale_conflict(postgres_review):
    Session, item_id = postgres_review
    before = snapshot(Session, item_id)
    other_actor = ACTOR.model_copy(update={"app_user_id": "reviewer-2", "monday_user_id": "101"})
    commands = [save_command(field_operations={label: {"operation": "specify_blank"}})
        for label in ("Decking", "Contact")]
    outcomes = race(Session, [
        lambda db: service.save_review_decisions(db, board_id=BOARD, item_id=item_id,
            actor=ACTOR, command=commands[0], source=source_snapshot()),
        lambda db: service.save_review_decisions(db, board_id=BOARD, item_id=item_id,
            actor=other_actor, command=commands[1], source=source_snapshot()),
    ])
    successes = [result for result in outcomes if isinstance(result, service.ReviewCommandResult)]
    errors = [result for result in outcomes if isinstance(result, BaseException)]
    assert len(successes) == len(errors) == 1, outcomes
    assert isinstance(errors[0], service.ReviewConflict) and errors[0].code == "stale_review"
    after = snapshot(Session, item_id)
    assert after["version"] == 1
    assert len(after["decisions"]) == 1
    assert after["generation"] == before["generation"] + 1
    assert after["save_events"] == 1


def test_concurrent_identical_retry_has_one_event_and_one_generation(postgres_review):
    Session, item_id = postgres_review
    before = snapshot(Session, item_id)
    request = save_command(key="same-command", field_operations={"Decking": {"operation": "specify_blank"}})

    def save(db):
        return service.save_review_decisions(db, board_id=BOARD, item_id=item_id,
            actor=ACTOR, command=request, source=source_snapshot())

    outcomes = race(Session, [save, save])
    assert all(isinstance(result, service.ReviewCommandResult) for result in outcomes), outcomes
    assert outcomes[0] == outcomes[1]
    after = snapshot(Session, item_id)
    assert after["version"] == 1
    assert after["save_events"] == 1
    assert after["generation"] == before["generation"] + 1


def test_concurrent_key_reuse_with_different_bodies_is_rejected(postgres_review):
    Session, item_id = postgres_review
    before = snapshot(Session, item_id)
    first = save_command(key="same-key", field_operations={"Decking": {"operation": "specify_blank"}})
    second = save_command(key="same-key", field_operations={"Decking": {
        "operation": "specify_value", "display_value": "Different request",
    }})
    outcomes = race(Session, [
        lambda db: service.save_review_decisions(db, board_id=BOARD, item_id=item_id,
            actor=ACTOR, command=first, source=source_snapshot()),
        lambda db: service.save_review_decisions(db, board_id=BOARD, item_id=item_id,
            actor=ACTOR, command=second, source=source_snapshot()),
    ])
    successes = [result for result in outcomes if isinstance(result, service.ReviewCommandResult)]
    errors = [result for result in outcomes if isinstance(result, BaseException)]
    assert len(successes) == len(errors) == 1, outcomes
    assert isinstance(errors[0], service.ReviewConflict) and errors[0].code == "idempotency_conflict"
    after = snapshot(Session, item_id)
    assert after["version"] == 1
    assert after["save_events"] == 1
    assert after["generation"] == before["generation"] + 1


def test_delayed_source_read_cannot_replace_a_newer_refresh(postgres_review):
    Session, item_id = postgres_review
    before = snapshot(Session, item_id)
    source_read = threading.Event()
    release_read = threading.Event()
    outcomes = Queue()

    def delayed_refresh():
        with Session() as db:
            try:
                previous_fingerprint = db.get(PerformanceSpecificationReview, (BOARD, item_id)).source_fingerprint
                db.rollback()  # A remote read never holds review/specification locks.
                source_read.set()
                assert release_read.wait(timeout=10)
                outcomes.put(service.synchronize_review_source(db, board_id=BOARD, item_id=item_id,
                    account_id=ACCOUNT, source=source_snapshot("delayed-source"),
                    expected_source_fingerprint=previous_fingerprint))
            except BaseException as exc:
                outcomes.put(exc)

    delayed = threading.Thread(target=delayed_refresh, daemon=True)
    delayed.start()
    try:
        assert source_read.wait(timeout=5)
        with Session() as db:
            newest = service.synchronize_review_source(db, board_id=BOARD, item_id=item_id,
                account_id=ACCOUNT, source=source_snapshot("newest-source"),
                expected_source_fingerprint="sources-1")
        assert newest.changed
    finally:
        release_read.set()
        delayed.join(timeout=15)
    assert not delayed.is_alive()
    stale = outcomes.get(timeout=1)
    assert isinstance(stale, service.ReviewConflict) and stale.code == "source_changed", stale
    after = snapshot(Session, item_id)
    assert after["source_fingerprint"] == "newest-source"
    assert after["source_events"] == 1
    assert after["generation"] == before["generation"] + 1
