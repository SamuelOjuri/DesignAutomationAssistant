"""Shared Monday lifecycle observations suppress ordinary design-worker reads."""

from dataclasses import replace
from datetime import timedelta
import uuid

from fastapi import HTTPException
import pytest

from backend.app.models import DesignProcessingItem, DesignProcessingJob, MondayItemAvailability, PerformanceSpecification
from backend.app.monday_client import MondayItemUnavailable, MondayGroupItem
from backend.app.services import design_processing_worker as worker
from backend.app.services import design_processing_reconciliation as reconciliation
from backend.app.services.monday_item_availability import (
    observe_item, observe_unavailable, record_retired, availability_decision, claim_due_probe,
)
from backend.tests.test_design_processing_phase6 import (
    db_session, _seed_publication, _publication_snapshot, PublicationGateway,
)
from backend.tests.test_design_processing_reconciliation_checks import (
    NOW, BOARD, LANDING, seed, run, fixtures, config,
)


def worker_run(db, gateway, storage):
    return worker.run_worker_once(db, worker_id="availability-test", access_token="test-token",
        gateway=gateway, analysis_client=object(), artifact_storage=storage, mode="enabled",
        recover_leases=False, heartbeat_interval_seconds=0)


@pytest.mark.parametrize("claim", [worker.claim_due_analysis_jobs, worker.claim_due_design_processing_jobs])
def test_shared_hold_filters_older_jobs_before_claim_limit(db_session, claim):
    for item_id, age in (("123", 2), ("124", 1)):
        db_session.add(DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id=item_id,
            state="scheduled", warnings_json=[]))
        db_session.add(DesignProcessingJob(id=uuid.uuid4(), board_id=BOARD, item_id=item_id,
            trigger_type="test", status="scheduled", scheduled_for=NOW - timedelta(hours=age),
            created_at=NOW - timedelta(hours=age), updated_at=NOW, attempt_count=0))
    db_session.flush()
    record_retired(db_session, BOARD, "123", access_token="test-token", observed_at=NOW)
    db_session.commit()
    claimed = claim(db_session, worker_id="test", limit=1, now=NOW, access_token="test-token")
    assert [job.item_id for job in claimed] == ["124"]
    assert db_session.query(DesignProcessingJob).filter_by(item_id="123").one().attempt_count == 0


def test_validated_missing_read_is_shared_and_does_not_burn_analysis_attempts(db_session, monkeypatch):
    _, _, job, storage, _ = _seed_publication(db_session)
    clock = [NOW]
    monkeypatch.setattr(worker, "utc_now", lambda: clock[0])
    calls = []
    class Missing:
        def fetch_target(self, item_id):
            calls.append(item_id)
            raise MondayItemUnavailable()
    # The fixture's scheduled timestamp is current wall-clock time.
    job.scheduled_for = NOW
    db_session.commit()
    assert worker_run(db_session, Missing(), storage).deferred == 1
    db_session.refresh(job)
    assert job.status == "retry_wait" and job.attempt_count == 0
    assert worker_run(db_session, Missing(), storage).claimed == 0 and calls == ["123"]
    clock[0] = NOW + timedelta(hours=1)
    assert worker_run(db_session, Missing(), storage).deferred == 1
    state = db_session.query(MondayItemAvailability).one()
    assert state.consecutive_misses == 2
    assert state.next_check_at.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(hours=7)
    assert job.attempt_count == 0 and calls == ["123", "123"]


def test_positive_board_move_cancels_registered_board_specification(db_session):
    identity, item, job, storage, _ = _seed_publication(db_session)
    db_session.add(PerformanceSpecification(board_id=BOARD, item_id="123", requested_generation=2,
        completed_generation=1, scheduled_for=NOW, current_revision="retained-pdf"))
    db_session.commit()
    gateway = PublicationGateway(replace(_publication_snapshot(identity), board_id="999999"))
    assert worker_run(db_session, gateway, storage).cancelled == 1
    specification = db_session.get(PerformanceSpecification, (BOARD, "123"))
    assert specification.scheduled_for is None and specification.current_revision == "retained-pdf"
    assert job.status == "cancelled" and item.state == "ineligible"
    assert db_session.query(MondayItemAvailability).one().state == "out_of_board"
    assert not any(event[0] in {"update", "upload"} for event in gateway.events)


def test_probe_claimed_by_another_consumer_defers_an_already_claimed_job(db_session):
    _, _, job, storage, _ = _seed_publication(db_session)
    job.scheduled_for = NOW
    db_session.commit()
    [claimed] = worker.claim_due_design_processing_jobs(db_session, worker_id="availability-test",
        access_token="test-token", now=NOW)
    assert claim_due_probe(db_session, BOARD, "123", access_token="test-token", now=NOW)
    db_session.commit()
    class NoReads:
        def fetch_target(self, item_id):
            pytest.fail("Another consumer owns the current availability probe")
    outcome = worker.execute_claimed_analysis_job(db_session, claimed.id,
        worker_id="availability-test", access_token="test-token", gateway=NoReads(),
        analysis_client=object(), artifact_storage=storage, mode="enabled", now=NOW,
        heartbeat_interval_seconds=0)
    assert outcome == "deferred" and job.status == "retry_wait"
    assert job.last_error == "probe_in_progress" and job.attempt_count == 0


def test_delayed_missing_response_cannot_release_a_newer_worker_lease(db_session):
    _, _, job, storage, _ = _seed_publication(db_session)
    class LeaseChangesDuringRead:
        def fetch_target(self, item_id):
            job.locked_by = "newer-worker"
            db_session.commit()
            raise MondayItemUnavailable()
    result = worker_run(db_session, LeaseChangesDuringRead(), storage)
    db_session.refresh(job)
    assert result.deferred == result.cancelled == 0
    assert job.status == "running" and job.locked_by == "newer-worker"


@pytest.mark.parametrize("status", [401, 403, 429, 502])
def test_api_failures_are_not_recorded_as_missing_items(db_session, status):
    _, _, _, storage, _ = _seed_publication(db_session)
    calls = []
    class Failing:
        def fetch_target(self, item_id):
            calls.append(item_id)
            raise HTTPException(status, "upstream request failed")
    worker_run(db_session, Failing(), storage)
    assert calls == ["123"]
    assert db_session.query(MondayItemAvailability).count() == 0


def test_shared_unavailability_from_another_worker_defers_design_reconciliation(db_session):
    seed(db_session)
    # Use the same registered identity for the shared observation.
    registered = db_session.query(DesignProcessingItem).one()
    observe_unavailable(db_session, BOARD, registered.item_id, access_token="test", observed_at=NOW)
    db_session.commit()
    class NoReads:
        def fetch_target(self, item_id):
            pytest.fail("Shared suppressed item triggered a full target lookup")
    result = run(db_session, NoReads(), now=NOW + timedelta(minutes=10))
    assert result.scanned == 0 and result.deferred == 1


def test_live_eligible_inventory_can_confirm_return_of_a_retired_registered_item(db_session):
    item = seed(db_session)
    record_retired(db_session, BOARD, item.item_id, access_token="test", observed_at=NOW)
    db_session.commit()
    gateway = fixtures.FakeReconciliationGateway({item.item_id: fixtures._snapshot(
        item_id=item.item_id, group_id="topics")})
    result = run(db_session, gateway, now=NOW + timedelta(minutes=1), discovered_items={
        "topics": [MondayGroupItem(item.item_id, (NOW - timedelta(days=40)).isoformat())],
    })
    assert result.queued == 1 and gateway.calls == [item.item_id]
    assert availability_decision(db_session, BOARD, item.item_id, access_token="test",
        now=NOW + timedelta(minutes=1)).allowed


def test_delayed_moved_snapshot_cannot_cancel_after_a_newer_restore(db_session, monkeypatch):
    identity, _, _, storage, _ = _seed_publication(db_session)
    monkeypatch.setattr(worker, "utc_now", lambda: NOW)
    db_session.query(DesignProcessingJob).one().scheduled_for = NOW
    db_session.add(PerformanceSpecification(board_id=BOARD, item_id="123", requested_generation=2,
        completed_generation=1, scheduled_for=NOW, current_revision="retained"))
    db_session.commit()
    active = _publication_snapshot(identity)
    class RestoreDuringRead(PublicationGateway):
        def fetch_target(self, item_id):
            if not self.events:
                observe_item(db_session, BOARD, {"id": item_id, "state": "active",
                    "board": {"id": BOARD}, "group": {"id": LANDING}},
                    access_token="test-token", observed_at=NOW + timedelta(seconds=1))
                db_session.commit()  # Simulate the independent restoration event.
                self.events.append(("delayed", item_id))
                return replace(active, board_id="999999")
            return super().fetch_target(item_id)
    worker_run(db_session, RestoreDuringRead(active), storage)
    spec = db_session.get(PerformanceSpecification, (BOARD, "123"))
    assert spec.scheduled_for is not None and spec.current_revision == "retained"
    assert db_session.query(MondayItemAvailability).one().state == "available"


def test_reconciliation_discards_delayed_deleted_snapshot_after_newer_restore(db_session):
    item = seed(db_session)
    class Delayed:
        def fetch_target(self, item_id):
            observe_item(db_session, BOARD, {"id": item_id, "state": "active",
                "board": {"id": BOARD}, "group": {"id": "topics"}},
                access_token="test", observed_at=NOW + timedelta(seconds=1))
            db_session.commit()
            return replace(fixtures._snapshot(item_id=item_id, group_id="topics"), item_state="deleted")
    result = run(db_session, Delayed())
    assert result.deferred == 1 and result.items[0].reason == "newer_availability_observation"
    assert db_session.query(MondayItemAvailability).one().state == "available"
    assert item.state == "ineligible"  # The delayed payload made no new workflow decision.
