"""Batched status contracts and shared probe ownership without live Monday calls."""

from datetime import timedelta

import pytest

from backend.app import monday_client
from backend.app.models import MondayItemAvailability
from backend.app.services import monday_item_availability as availability
from backend.app.services.monday_item_probes import probe_items
from backend.tests.test_monday_item_availability import BOARD, ITEM, TOKEN, db, item


def test_many_missing_ids_share_one_request_then_no_requests_until_due(db):
    calls = []
    fetch = lambda ids: calls.append(ids) or {}
    assert probe_items(db, BOARD, [ITEM, "124", ITEM], fetch_statuses=fetch) == {ITEM: None, "124": None}
    assert calls == [[ITEM, "124"]]
    assert probe_items(db, BOARD, [ITEM, "124"], fetch_statuses=fetch) == {}
    assert len(calls) == 1
    assert all(row.state == "unavailable" and row.consecutive_misses == 1
               and row.probe_token is None for row in db.query(MondayItemAvailability).all())


def test_batches_over_100_are_split_without_loading_assets(db):
    calls = []
    identities = [str(number) for number in range(1, 102)]
    assert len(probe_items(db, BOARD, identities, fetch_statuses=lambda ids: calls.append(ids) or {})) == 101
    assert [len(batch) for batch in calls] == [100, 1]


def test_invalid_partial_adapter_result_never_records_a_missing_item(db):
    with pytest.raises(ValueError):
        probe_items(db, BOARD, [ITEM, "124"], fetch_statuses=lambda ids: {ITEM: {"id": ITEM}})
    assert all(row.state == "unknown" and row.consecutive_misses == 0 and row.probe_token is None
               for row in db.query(MondayItemAvailability).all())


def test_failed_batch_releases_leases_without_recording_unavailable(db):
    def fail(ids):
        raise monday_client.TransientMondayAPIError(detail="throttled")
    with pytest.raises(monday_client.TransientMondayAPIError):
        probe_items(db, BOARD, [ITEM], fetch_statuses=fail)
    row = db.query(MondayItemAvailability).one()
    assert row.state == "unknown" and row.consecutive_misses == 0 and row.probe_token is None


def test_late_batch_response_cannot_override_newer_restore_or_reach_consumers(db):
    def delayed(ids):
        availability.observe_item(db, BOARD, item(), observed_at=availability.utc_now() + timedelta(seconds=1))
        db.commit()
        return {ITEM: item(board="999")}
    assert probe_items(db, BOARD, [ITEM], fetch_statuses=delayed) == {}
    row = db.query(MondayItemAvailability).one()
    assert row.state == "available" and row.actual_board_id == BOARD and row.probe_token is None


def test_expired_probe_does_not_record_or_return_old_status(db):
    def delayed(ids):
        row = db.query(MondayItemAvailability).one()
        row.probe_until = availability.utc_now() - timedelta(seconds=1)
        db.commit()
        return {}
    assert probe_items(db, BOARD, [ITEM], fetch_statuses=delayed) == {}
    assert db.query(MondayItemAvailability).one().state == "unknown"


def test_dry_run_does_not_write_observations_or_leases(db):
    assert probe_items(db, BOARD, [ITEM], fetch_statuses=lambda ids: {}, dry_run=True) == {ITEM: None}
    assert db.query(MondayItemAvailability).count() == 0


def test_retired_item_never_calls_monday_without_positive_rediscovery(db):
    availability.record_retired(db, BOARD, ITEM)
    db.commit()
    fetch = lambda ids: pytest.fail("retired item was read")
    assert probe_items(db, BOARD, [ITEM], fetch_statuses=fetch) == {}
    assert probe_items(db, BOARD, [ITEM], fetch_statuses=fetch, force=True) == {}
    assert probe_items(db, BOARD, [ITEM], fetch_statuses=lambda ids: {ITEM: item()},
                       rediscovered_ids=[ITEM]) == {ITEM: item()}
    assert db.query(MondayItemAvailability).one().state == "available"


def test_inventory_does_not_reset_unavailable_backoff(db):
    availability.observe_unavailable(db, BOARD, ITEM)
    db.commit()
    assert probe_items(db, BOARD, [ITEM], rediscovered_ids=[ITEM],
                       fetch_statuses=lambda ids: pytest.fail("backoff was bypassed")) == {}


def test_status_query_batches_and_explicitly_includes_inactive_items(monkeypatch):
    calls = []
    def request(token, query, variables, **kwargs):
        calls.append((token, query, variables))
        return {"data": {"items": [item()]}}
    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    assert monday_client.fetch_monday_item_statuses(TOKEN, [ITEM, "124", ITEM]) == {ITEM: item()}
    assert calls[0][2] == {"itemIds": [ITEM, "124"]}
    assert "exclude_nonactive: false" in calls[0][1] and "limit: 100" in calls[0][1]
    assert "assets" not in calls[0][1] and "column_values" not in calls[0][1]


@pytest.mark.parametrize("payload", [
    None, {}, {"data": {}}, {"data": {"items": None}},
    {"data": {"items": []}, "errors": [{"message": "not authorized"}]},
    {"data": {"items": [item(), item()]}},
    {"data": {"items": [{**item(), "id": "999"}]}},
    {"data": {"items": [{**item(), "board": None}]}},
    {"data": {"items": [{**item(), "state": "unknown"}]}},
    {"data": {"items": [{**item(), "group": None}]}},
])
def test_partial_or_malformed_status_responses_are_errors(monkeypatch, payload):
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs: payload)
    with pytest.raises(monday_client.MondayReadContractError):
        monday_client.fetch_monday_item_statuses(TOKEN, [ITEM])


def test_status_batches_reject_more_than_100_ids_before_network(monkeypatch):
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs: pytest.fail("network"))
    with pytest.raises(ValueError):
        monday_client.fetch_monday_item_statuses(TOKEN, [str(number) for number in range(1, 102)])
