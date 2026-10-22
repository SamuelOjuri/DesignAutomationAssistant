from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from backend.app import monday_client
from backend.app.config import settings
from backend.app.models import DesignProcessingItem, DesignProcessingReconciliationCheck, Task
from backend.app.services import auto_sync_reconciliation, design_processing_reconciliation, monday_reconciliation
from backend.tests import test_design_processing_phase4 as fixtures


db_session = fixtures.db_session


def test_account_identity_is_reused_across_distinct_events_and_token_rotation(monkeypatch):
    monday_client.fetch_current_account_id.cache_clear()
    calls = []
    def request(token, query, variables=None, **kwargs):
        calls.append((token, query))
        if query == monday_client.CURRENT_ACCOUNT_QUERY:
            return {"data": {"me": {"account": {"id": f"account-{token}"}}}}
        return {"data": {"items": [{"id": variables["itemIds"][0]}]}}
    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    try:
        for token, item_id in [("old", "1"), ("old", "2"), ("new", "3")]:
            result = monday_client.fetch_current_source_revision_inputs(token, item_id)
            assert result["account_id"] == f"account-{token}"
        assert sum(query == monday_client.CURRENT_ACCOUNT_QUERY for _, query in calls) == 2
        assert len(calls) == 5
    finally:
        monday_client.fetch_current_account_id.cache_clear()


def test_account_identity_failures_are_not_cached(monkeypatch):
    monday_client.fetch_current_account_id.cache_clear()
    calls = []
    def request(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise HTTPException(502, "temporary failure")
        return {"data": {"me": {"account": {"id": "account"}}}}
    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    try:
        with pytest.raises(HTTPException):
            monday_client.fetch_current_account_id("token")
        assert monday_client.fetch_current_account_id("token") == "account"
        assert len(calls) == 2
    finally:
        monday_client.fetch_current_account_id.cache_clear()


def test_cached_identity_still_requires_remote_item_authorization(monkeypatch):
    monday_client.fetch_current_account_id.cache_clear()
    item_requests = []
    def request(token, query, variables=None, **kwargs):
        if query == monday_client.CURRENT_ACCOUNT_QUERY:
            return {"data": {"me": {"account": {"id": "account"}}}}
        item_requests.append(variables["itemIds"])
        if len(item_requests) == 2:
            raise HTTPException(403, "token revoked")
        return {"data": {"items": [{"id": "1"}]}}
    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    try:
        monday_client.fetch_current_source_revision_inputs("token", "1")
        with pytest.raises(HTTPException) as failure:
            monday_client.fetch_current_source_revision_inputs("token", "1")
        assert failure.value.status_code == 403
        assert len(item_requests) == 2
    finally:
        monday_client.fetch_current_account_id.cache_clear()


@pytest.mark.parametrize("separate_boards", [False, True])
def test_combined_reconciliation_shares_live_discovery_without_writing_in_dry_run(db_session, monkeypatch, separate_boards):
    monkeypatch.setattr(settings, "auto_sync_board_id", fixtures.BOARD_ID)
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics")
    monkeypatch.setattr(settings, "auto_sync_excluded_group_ids", fixtures.LANDING_GROUP_ID)
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")
    monkeypatch.setattr(settings, "auto_sync_enabled", True)
    monkeypatch.setattr(settings, "design_processing_mode", "shadow")
    monkeypatch.setattr(settings, "design_processing_board_id", "other" if separate_boards else fixtures.BOARD_ID)
    monkeypatch.setattr(settings, "design_processing_landing_group_id", fixtures.LANDING_GROUP_ID)
    monkeypatch.setattr(settings, "design_processing_activation_timestamp", fixtures.NOW - timedelta(days=1))
    calls = []
    def discover(token, board_id, group_ids, *, limit):
        calls.append((board_id, group_ids, limit))
        return {group_id: [monday_client.MondayGroupItem(
            "1" if group_id == "topics" else "2", fixtures.NOW.isoformat(),
        )] for group_id in group_ids}
    monkeypatch.setattr(monday_reconciliation, "list_items_in_groups", discover)
    monkeypatch.setattr(auto_sync_reconciliation, "list_item_ids_in_groups", lambda *args, **kwargs: pytest.fail("duplicate discovery"))
    monkeypatch.setattr(design_processing_reconciliation, "list_items_in_groups", lambda *args, **kwargs: pytest.fail("duplicate discovery"))
    monkeypatch.setattr(auto_sync_reconciliation, "fetch_current_account_id", lambda token: "acct")
    def source(token, item_id, **kwargs):
        return {**fixtures._monday_item(item_id=item_id, group_id="topics"), "account_id": "acct"}
    monkeypatch.setattr(auto_sync_reconciliation, "fetch_current_source_revision_inputs", source)
    targets = {item_id: replace(fixtures._snapshot(item_id=item_id), board_id=str(settings.design_processing_board_id)) for item_id in ("1", "2")}
    gateway = fixtures.FakeReconciliationGateway(targets)
    monkeypatch.setattr(design_processing_reconciliation, "MondayDesignProcessingReadGateway", lambda **kwargs: gateway)
    for _ in range(2):
        results = monday_reconciliation.reconcile_monday_once(db_session, access_token="token", page_size=50)
        assert results["active"].scanned == 1 and results["design"].scanned == 2
        assert not any(result.errors for result in results.values())
    assert len(calls) == (4 if separate_boards else 2)
    assert all(limit == 50 for _, _, limit in calls)
    assert all(len(groups) == len(set(groups)) for _, groups, _ in calls)
    assert db_session.query(Task).count() == 0
    assert db_session.query(DesignProcessingItem).count() == 0
    assert db_session.query(DesignProcessingReconciliationCheck).count() == 0


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"design_limit": 0}, {"completed_limit": 0}, {"page_size": 501}])
def test_combined_invalid_limits_do_not_call_monday(db_session, monkeypatch, kwargs):
    monkeypatch.setattr(monday_reconciliation, "get_monday_ingestion_access_token", lambda: pytest.fail("invalid limits"))
    with pytest.raises(ValueError):
        monday_reconciliation.reconcile_monday_once(db_session, **kwargs)


@pytest.mark.parametrize("failed_scope", [None, "active", "completed", "design"])
def test_combined_command_reports_failures_and_closes_session(monkeypatch, capsys, failed_scope):
    closed = []
    monkeypatch.setattr(monday_reconciliation, "SessionLocal", lambda: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr("sys.argv", ["monday_reconciliation", "--dry-run"])
    results = {
        "active": auto_sync_reconciliation.ReconciliationResult(dry_run=True, board_id="board", errors=int(failed_scope == "active")),
        "completed": auto_sync_reconciliation.ReconciliationResult(dry_run=True, board_id="board", errors=int(failed_scope == "completed")),
        "design": design_processing_reconciliation.DesignProcessingReconciliationResult(dry_run=True, board_id="board", mode="shadow", errors=int(failed_scope == "design")),
    }
    monkeypatch.setattr(monday_reconciliation, "reconcile_monday_once", lambda db, **kwargs: results)
    assert monday_reconciliation.main() == int(failed_scope is not None)
    assert '"design"' in capsys.readouterr().out
    assert closed == [True]
