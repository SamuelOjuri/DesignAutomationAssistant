"""Specification queues share the same lifecycle evidence as other workers."""

from copy import deepcopy
from datetime import timedelta

import pytest

from backend.app.models import DesignProcessingReconciliationCheck, MondayItemAvailability
from backend.app.monday_client import MondayItemUnavailable
from backend.app.services import performance_specification as worker
from backend.app.services.monday_item_availability import observe_item, observe_unavailable, record_retired
from backend.tests.test_performance_specification import BOARD, ITEM, case, current, run


def queue_and_claim(case):
    worker.enqueue_specification(case.db, BOARD, ITEM, immediate=True)
    case.db.commit()
    claim = worker.claim_specification(case.db)
    assert claim is not None
    return claim


def execute(case, claim):
    return worker.execute_specification(case.db, claim, gateway=case.gateway,
        client=case.client, storage=case.storage)


def forbid_remote(case):
    def unexpected(*args, **kwargs):
        pytest.fail("Suppressed specification performed a remote read or mutation")
    for name in ("fetch_target", "fetch_holding", "fetch_project", "inspect_files", "upload", "delete"):
        setattr(case.gateway, name, unexpected)


@pytest.mark.parametrize("legacy", [False, True])
def test_retired_queued_discovery_claim_and_execution_make_zero_remote_reads(case, legacy):
    claim = queue_and_claim(case)
    if legacy:
        case.db.add(DesignProcessingReconciliationCheck(board_id=BOARD, item_id=ITEM,
            last_attempted_at=worker.utc_now(), last_outcome="retired", last_reason="confirmed_deleted"))
    else:
        record_retired(case.db, BOARD, ITEM)
    case.db.commit()
    forbid_remote(case)

    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    assert worker.claim_specification(case.db) is None
    assert execute(case, claim) == "deferred"
    record = current(case)
    assert record.scheduled_for is None and record.lease_token is None
    assert record.attempt_count == 0 and not case.calls and not case.storage.objects


def test_shared_missing_observation_defers_discovery_and_claimed_execution_without_attempt_burn(case):
    claim = queue_and_claim(case)
    observed = observe_unavailable(case.db, BOARD, ITEM)
    deadline = observed.next_check_at
    case.db.commit()
    forbid_remote(case)

    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    assert worker.claim_specification(case.db) is None
    assert execute(case, claim) == "deferred"
    record = current(case)
    assert worker._aware(record.scheduled_for) == deadline
    assert record.attempt_count == 0 and record.lease_token is None
    assert record.requested_generation > record.completed_generation
    assert not case.calls and not case.storage.objects


def test_typed_missing_during_source_read_uses_shared_deadline_without_model_or_retry_attempt(case):
    claim = queue_and_claim(case)
    calls = []
    def missing(item_id):
        calls.append(item_id)
        raise MondayItemUnavailable()
    case.gateway.fetch_target = missing

    assert execute(case, claim) == "deferred"

    record = current(case)
    observed = case.db.query(MondayItemAvailability).one()
    assert observed.state == "unavailable" and observed.consecutive_misses == 1
    assert worker._aware(record.scheduled_for) == worker._aware(observed.next_check_at)
    assert record.attempt_count == 0 and not case.calls
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    assert worker.run_specifications_once(case.db, gateway=case.gateway,
        client=case.client, storage=case.storage) == []
    assert calls == [ITEM]


@pytest.mark.parametrize("change", ["archived", "out_of_board"])
def test_positive_ineligible_observation_preserves_published_pdf_and_stops_work(case, change):
    assert run(case) == ["published"]
    revision = current(case).current_revision
    asset = case.gateway.uploads[0][0].asset_id
    if change == "archived":
        case.gateway.target.item_state = "archived"
        case.gateway.holding["state"] = "archived"
    else:
        case.gateway.target.board_id = "999999"
        case.gateway.holding["board"]["id"] = "999999"

    assert run(case) == ["excluded"]

    record = current(case)
    assert record.scheduled_for is None and record.lease_token is None
    assert record.current_revision == revision and record.attempt_count == 0
    assert asset in {entry.asset_id for entry in case.gateway.files}
    assert len(case.gateway.uploads) == 1 and not case.gateway.deletes
    assert case.db.query(MondayItemAvailability).one().state == change
    forbid_remote(case)
    assert worker.claim_specification(case.db) is None
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0


@pytest.mark.parametrize("stale_state", ["deleted", "out_of_board"])
def test_delayed_ineligible_read_after_newer_restore_cannot_cancel_current_specification(
    case, monkeypatch, stale_state,
):
    assert run(case) == ["published"]
    revision = current(case).current_revision
    instant = worker.utc_now() + timedelta(minutes=1)
    monkeypatch.setattr(worker, "utc_now", lambda: instant)
    claim = queue_and_claim(case)
    stale = deepcopy(case.gateway.target)
    if stale_state == "deleted":
        stale.item_state = "deleted"
    else:
        stale.board_id = "999999"
    def restore_during_read(item_id):
        observe_item(case.db, BOARD, {"id": ITEM, "state": "active",
            "board": {"id": BOARD}, "group": {"id": case.gateway.target.group_id}},
            observed_at=instant + timedelta(seconds=1))
        case.db.commit()  # A separate restoration event wins while this read is in flight.
        return stale
    case.gateway.fetch_target = restore_during_read

    assert execute(case, claim) == "superseded"

    record = current(case)
    assert record.current_revision == revision and record.scheduled_for is not None
    assert record.requested_generation > record.completed_generation
    assert record.attempt_count == 0 and len(case.gateway.uploads) == 1
    assert case.db.query(MondayItemAvailability).one().state == "available"
