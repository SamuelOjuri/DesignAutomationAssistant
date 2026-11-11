"""Shared read suppression, principal isolation and recovery without Monday I/O."""

from datetime import datetime, timedelta, timezone
import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, literal, select
from sqlalchemy.orm import Session

from backend.app.config import Settings, settings
from backend.app.db import Base
from backend.app.models import AutoSyncReconciliationCheck, DesignProcessingReconciliationCheck, MondayItemAvailability
from backend.app.services import monday_item_availability as availability


NOW = datetime(2026, 11, 10, 16, tzinfo=timezone.utc)
BOARD, ITEM, TOKEN = "1882196103", "123", "offline-processing-token"
TABLES = [model.__table__ for model in (
    MondayItemAvailability, AutoSyncReconciliationCheck, DesignProcessingReconciliationCheck,
)]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(settings, "monday_ingestion_access_token", TOKEN)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=TABLES)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def item(*, state="active", board=BOARD, group="landing"):
    return {"id": ITEM, "state": state, "board": {"id": board}, "group": {"id": group}}


def permitted(db, *, now=NOW, token=TOKEN, rediscovered=False):
    statement = select(literal(ITEM)).where(availability.availability_read_predicate(
        BOARD, ITEM, token, now, rediscovered=rediscovered,
    ))
    return db.scalar(statement) is not None


def test_processing_identity_never_stores_the_token_and_rotated_tokens_do_not_share_misses(db):
    availability.observe_unavailable(db, BOARD, ITEM, observed_at=NOW)
    row = db.query(MondayItemAvailability).one()
    assert row.processing_identity == availability.processing_identity(TOKEN)
    assert TOKEN not in row.processing_identity and len(row.processing_identity) == 64
    assert not availability.availability_decision(db, BOARD, ITEM, TOKEN, NOW).allowed
    assert availability.availability_decision(db, BOARD, ITEM, "another-token", NOW).allowed
    assert permitted(db, token="another-token")


def test_empty_reads_increase_shared_backoff_without_becoming_deletion(db):
    now = NOW
    for index, delay in enumerate([3600, 21600, 86400, 604800, 604800], start=1):
        row = availability.observe_unavailable(db, BOARD, ITEM, observed_at=now)
        assert row.state == "unavailable" and row.consecutive_misses == index
        assert row.retired_at is None
        decision = availability.availability_decision(db, BOARD, ITEM, now=now)
        assert not decision.allowed and decision.next_check_at == now + timedelta(seconds=delay)
        assert not permitted(db, now=now)
        assert not permitted(db, now=now, rediscovered=True)
        now += timedelta(seconds=delay)
        assert availability.availability_decision(db, BOARD, ITEM, now=now).allowed
        assert permitted(db, now=now)


def test_positive_read_restores_availability_and_resets_misses(db):
    availability.observe_unavailable(db, BOARD, ITEM, observed_at=NOW)
    row = availability.observe_item(db, BOARD, item(), observed_at=NOW + timedelta(seconds=1))
    assert row.state == "available" and row.consecutive_misses == 0 and row.next_check_at is None
    assert row.actual_board_id == BOARD and row.actual_group_id == "landing"
    assert availability.availability_decision(db, BOARD, ITEM, now=NOW + timedelta(seconds=2)).allowed
    assert permitted(db, now=NOW + timedelta(seconds=2))


@pytest.mark.parametrize("snapshot,state", [(item(state="deleted"), "deleted"),
    (item(state="archived"), "archived"), (item(board="999"), "out_of_board")])
def test_positive_exclusions_suppress_reads_until_recheck_or_rediscovery(db, snapshot, state):
    row = availability.observe_item(db, BOARD, snapshot, observed_at=NOW)
    assert row.state == state
    assert not availability.availability_decision(db, BOARD, ITEM, now=NOW).allowed
    assert not permitted(db)
    assert permitted(db, rediscovered=True)
    lease = availability.claim_due_probe(db, BOARD, ITEM, now=NOW, rediscovered=True)
    assert lease
    assert not availability.claim_due_probe(db, BOARD, ITEM, now=NOW, rediscovered=True)
    assert availability.release_probe(db, BOARD, ITEM, lease)
    future = NOW + timedelta(seconds=settings.monday_item_availability_inactive_recheck_seconds)
    assert availability.availability_decision(db, BOARD, ITEM, now=future).allowed


def test_legacy_retirement_stops_all_consumers_and_current_positive_read_can_restore(db):
    db.add(DesignProcessingReconciliationCheck(board_id=BOARD, item_id=ITEM,
        last_attempted_at=NOW, last_checked_at=NOW, last_outcome="retired", last_reason="confirmed_deleted"))
    db.flush()
    assert not availability.availability_decision(db, BOARD, ITEM, now=NOW).allowed
    assert not permitted(db)
    assert not availability.request_retry(db, BOARD, ITEM, observed_at=NOW + timedelta(days=1))
    assert permitted(db, rediscovered=True)
    lease = availability.claim_due_probe(db, BOARD, ITEM, now=NOW + timedelta(seconds=1), rediscovered=True)
    assert lease
    availability.observe_unavailable(db, BOARD, ITEM, observed_at=NOW + timedelta(seconds=2))
    availability.release_probe(db, BOARD, ITEM, lease)
    assert not availability.availability_decision(db, BOARD, ITEM, now=NOW + timedelta(days=10)).allowed
    availability.observe_item(db, BOARD, item(), observed_at=NOW + timedelta(days=11))
    assert availability.availability_decision(db, BOARD, ITEM, now=NOW + timedelta(days=11)).allowed
    assert permitted(db, now=NOW + timedelta(days=11))


def test_auto_sync_legacy_retirement_is_also_honored(db):
    db.add(AutoSyncReconciliationCheck(board_id=BOARD, item_id=ITEM, scope="completed_transition",
        last_attempted_at=NOW, last_checked_at=NOW, last_outcome="retired", last_reason="confirmed_deleted"))
    db.flush()
    assert not availability.availability_decision(db, BOARD, ITEM, now=NOW).allowed
    assert not permitted(db)


def test_confirmed_retirement_is_terminal_for_retries_but_not_positive_restoration(db):
    row = availability.record_retired(db, BOARD, ITEM, observed_at=NOW)
    assert row.state == "retired"
    assert not availability.availability_decision(db, BOARD, ITEM, now=NOW + timedelta(days=99)).allowed
    assert not availability.request_retry(db, BOARD, ITEM, observed_at=NOW + timedelta(days=99))
    assert not availability.claim_due_probe(db, BOARD, ITEM, now=NOW + timedelta(days=99))
    availability.observe_unavailable(db, BOARD, ITEM, observed_at=NOW + timedelta(days=99))
    assert row.state == "retired"
    availability.observe_item(db, BOARD, item(), observed_at=NOW + timedelta(days=100))
    assert row.state == "available"


def test_operator_retry_releases_nonterminal_delay_without_erasing_miss_history(db):
    availability.observe_unavailable(db, BOARD, ITEM, observed_at=NOW)
    assert availability.request_retry(db, BOARD, ITEM, observed_at=NOW + timedelta(seconds=1))
    decision = availability.availability_decision(db, BOARD, ITEM, now=NOW + timedelta(seconds=1))
    assert decision.allowed
    assert db.query(MondayItemAvailability).one().consecutive_misses == 1


def test_older_event_and_inflight_read_cannot_overwrite_a_newer_observation(db):
    availability.observe_item(db, BOARD, item(), observed_at=NOW + timedelta(minutes=1))
    row = availability.observe_item(db, BOARD, item(state="deleted"),
        observed_at=NOW + timedelta(minutes=2), event_at=NOW)
    assert row.state == "available"
    availability.observe_unavailable(db, BOARD, ITEM, observed_at=NOW + timedelta(minutes=3))
    availability.observe_item(db, BOARD, item(), observed_at=NOW + timedelta(minutes=2))
    assert row.state == "unavailable"
    assert not availability.request_retry(db, BOARD, ITEM, observed_at=NOW + timedelta(minutes=4), event_at=NOW)


def test_shared_probe_lease_is_atomic_and_old_owners_cannot_release_successors(db):
    first = availability.claim_due_probe(db, BOARD, ITEM, now=NOW)
    assert first and not availability.claim_due_probe(db, BOARD, ITEM, now=NOW)
    assert not permitted(db)
    assert not availability.release_probe(db, BOARD, ITEM, "wrong-owner")
    later = NOW + timedelta(seconds=settings.monday_item_availability_probe_lease_seconds)
    second = availability.claim_due_probe(db, BOARD, ITEM, now=later)
    assert second and first != second
    assert not availability.release_probe(db, BOARD, ITEM, first)
    assert availability.release_probe(db, BOARD, ITEM, second)
    assert permitted(db, now=later)


def test_expired_or_reclaimed_probe_cannot_publish_its_result(db, monkeypatch):
    first = availability.claim_due_probe(db, BOARD, ITEM, now=NOW)
    after_lease = NOW + timedelta(seconds=settings.monday_item_availability_probe_lease_seconds + 1)
    monkeypatch.setattr(availability, "utc_now", lambda: after_lease)
    assert not availability.complete_probe(db, BOARD, ITEM, first, item(), observed_at=NOW)
    second = availability.claim_due_probe(db, BOARD, ITEM, now=after_lease)
    assert second
    assert not availability.complete_probe(db, BOARD, ITEM, first, None, observed_at=NOW)
    assert availability.complete_probe(db, BOARD, ITEM, second, item(), observed_at=after_lease)
    assert db.query(MondayItemAvailability).one().state == "available"


def test_newer_restore_observation_prevents_probe_from_returning_stale_board_move(db, monkeypatch):
    lease = availability.claim_due_probe(db, BOARD, ITEM, now=NOW)
    restored = NOW + timedelta(seconds=2)
    availability.observe_item(db, BOARD, item(), observed_at=restored)
    monkeypatch.setattr(availability, "utc_now", lambda: restored + timedelta(seconds=1))
    assert not availability.complete_probe(db, BOARD, ITEM, lease, item(board="999"), observed_at=NOW)
    row = db.query(MondayItemAvailability).one()
    assert row.state == "available" and row.actual_board_id == BOARD


def test_missing_probe_never_downgrades_confirmed_legacy_retirement(db, monkeypatch):
    db.add(DesignProcessingReconciliationCheck(board_id=BOARD, item_id=ITEM,
        last_attempted_at=NOW, last_checked_at=NOW, last_outcome="retired", last_reason="confirmed_deleted"))
    db.flush()
    started = NOW + timedelta(seconds=1)
    lease = availability.claim_due_probe(db, BOARD, ITEM, now=started, rediscovered=True)
    monkeypatch.setattr(availability, "utc_now", lambda: started + timedelta(seconds=1))
    assert lease and not availability.complete_probe(db, BOARD, ITEM, lease, None, observed_at=started)
    assert not availability.availability_decision(db, BOARD, ITEM, now=started).allowed


def test_only_newer_lifecycle_event_can_request_retired_item_confirmation(db):
    availability.record_retired(db, BOARD, ITEM, observed_at=NOW)
    later = NOW + timedelta(minutes=1)
    assert not availability.lifecycle_probe_allowed(db, BOARD, ITEM, None, observed_at=later)
    assert not availability.lifecycle_probe_allowed(db, BOARD, ITEM, NOW, observed_at=later)
    assert not availability.lifecycle_probe_allowed(db, BOARD, ITEM, NOW - timedelta(days=1), observed_at=later)
    assert not availability.lifecycle_probe_allowed(db, BOARD, ITEM, later + timedelta(minutes=6), observed_at=later)
    assert availability.lifecycle_probe_allowed(db, BOARD, ITEM, later, observed_at=later)
    assert not availability.availability_decision(db, BOARD, ITEM, now=later).allowed


def test_reads_and_observations_do_not_commit_callers_transaction(db):
    availability.observe_unavailable(db, BOARD, ITEM, observed_at=NOW)
    assert db.in_transaction()
    db.rollback()
    assert db.query(MondayItemAvailability).count() == 0


def test_sql_gate_filters_suppressed_items_before_the_batch_limit(db):
    for item_id in (ITEM, "124"):
        db.add(DesignProcessingReconciliationCheck(board_id=BOARD, item_id=item_id,
            last_attempted_at=NOW, last_outcome="ignored", last_reason="ready"))
    db.flush()
    availability.observe_unavailable(db, BOARD, ITEM, observed_at=NOW)
    check = DesignProcessingReconciliationCheck
    query = select(check.item_id).where(check.board_id == BOARD,
        availability.availability_read_predicate(check.board_id, check.item_id, now=NOW)
    ).order_by(check.item_id).limit(1)
    assert db.scalar(query) == "124"


@pytest.mark.parametrize("bad", [{"id":ITEM}, item(state="unknown"), item(board="bad"), item(group=None)])
def test_incomplete_positive_reads_cannot_establish_availability(db, bad):
    with pytest.raises(ValueError):
        availability.observe_item(db, BOARD, bad, observed_at=NOW)
    assert db.query(MondayItemAvailability).count() == 0


def test_migration_round_trip_and_history_independence():
    engine = create_engine("sqlite://")
    migration = importlib.import_module("backend.migrations.versions.0020_monday_item_availability")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            metadata = inspect(connection)
            assert metadata.get_pk_constraint("monday_item_availability")["constrained_columns"] == [
                "board_id", "item_id", "processing_identity",
            ]
            assert metadata.get_foreign_keys("monday_item_availability") == []
            migration.downgrade()
            assert "monday_item_availability" not in inspect(connection).get_table_names()
    engine.dispose()


@pytest.mark.parametrize("delays", [[0], [10, 5], [10, 10], []])
def test_retry_configuration_rejects_invalid_schedule(delays):
    values = settings.model_dump()
    values["monday_item_availability_retry_seconds"] = delays
    with pytest.raises(ValueError):
        Settings.model_validate(values)
