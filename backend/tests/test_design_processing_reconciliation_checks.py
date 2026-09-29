from datetime import timedelta
import importlib
import json
from types import SimpleNamespace
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from backend.app import monday_client
from backend.app.config import settings
from backend.app.models import (
    DesignProcessingItem, DesignProcessingJob, DesignProcessingReconciliationCheck, Task,
)
from backend.app.services import design_processing_reconciliation as reconciliation
from backend.app.services.design_processing_queue import queue_design_processing_snapshot
from backend.app.services.design_processing_target import MondayDesignProcessingReadGateway
from backend.tests import test_design_processing_phase4 as fixtures


db_session = fixtures.db_session
NOW = fixtures.NOW
BOARD = fixtures.BOARD_ID
LANDING = fixtures.LANDING_GROUP_ID
IDS = ("3146597919", "3149758340", "3156229911", "3159199922", "3161931760", "3181735459")


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings, "design_processing_board_id", BOARD)
    monkeypatch.setattr(settings, "design_processing_landing_group_id", LANDING)
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")
    monkeypatch.setattr(settings, "design_processing_unavailable_recheck_seconds", 3600)
    monkeypatch.setattr(settings, "design_processing_excluded_recheck_seconds", 21600)
    monkeypatch.setattr(reconciliation, "list_items_in_groups", lambda *args, **kwargs: {})


def seed(db, item_id=IDS[0], *, state="ineligible", age_days=40):
    item = DesignProcessingItem(
        id=uuid.uuid4(), board_id=BOARD, item_id=item_id, state=state,
        extracted_parameters_json={"Project Name": "Retained extraction"},
        match_result_json={"matches": []}, warnings_json=[],
        created_at=NOW - timedelta(days=age_days), updated_at=NOW - timedelta(days=age_days),
    )
    db.add(item)
    db.commit()
    return item


def run(db, gateway, **kwargs):
    args = dict(
        dry_run=False, access_token="test", gateway=gateway, mode="enabled",
        activation_timestamp=NOW - timedelta(days=1), now=NOW,
    )
    args.update(kwargs)
    return reconciliation.reconcile_landing_zone_once(db, **args)


def check(db, item_id=IDS[0]):
    db.expire_all()
    return db.get(DesignProcessingReconciliationCheck, (BOARD, item_id))


def aware(value):
    return value.replace(tzinfo=NOW.tzinfo)


def test_broad_scan_admits_unregistered_active_items_across_pages_and_groups(db_session, monkeypatch):
    first_id, second_id, other_active_id = "3249971235", "3249996676", "3"
    boundary = NOW - timedelta(days=1)
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", f" topics,active_b,topics,{LANDING},completed ")
    monkeypatch.setattr(reconciliation, "list_items_in_groups", monday_client.list_items_in_groups)
    requests = []

    def summary(item_id, created_at):
        return {"id": item_id, "created_at": created_at.isoformat()}

    def request(token, query, variables, **kwargs):
        requests.append(variables)
        if "cursor" in variables:
            assert variables["cursor"] == "active-page-2"
            return {"data": {"next_items_page": {
                "cursor": None, "items": [summary(second_id, NOW - timedelta(hours=2))],
            }}}
        assert variables["boardIds"] == [BOARD]
        assert set(variables["groupIds"]) == {LANDING, "topics", "active_b"}
        assert len(variables["groupIds"]) == 3
        return {"data": {"boards": [{"groups": [
            {"id": LANDING, "items_page": {"cursor": None, "items": [
                # The same item appears twice after moving during pagination.
                summary(first_id, boundary),
            ]}},
            {"id": "topics", "items_page": {"cursor": "active-page-2", "items": [
                summary(first_id, boundary),
                summary("old", boundary - timedelta(seconds=1)),
            ]}},
            {"id": "active_b", "items_page": {"cursor": None, "items": [
                summary(other_active_id, NOW - timedelta(hours=1)),
            ]}},
        ]}]}}

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    gateway = fixtures.FakeReconciliationGateway({
        first_id: fixtures._snapshot(item_id=first_id, group_id="topics"),
        second_id: fixtures._snapshot(item_id=second_id, group_id="topics"),
        other_active_id: fixtures._snapshot(item_id=other_active_id, group_id="active_b"),
    })
    preview = run(db_session, gateway, dry_run=True)
    assert (preview.scanned, preview.queued, preview.skipped, preview.errors) == (3, 3, 1, 0)
    assert preview.items[0].reason == "before_activation_timestamp"
    assert all(item.action == "would_queued" for item in preview.items[1:])
    db_session.commit()
    assert db_session.query(DesignProcessingItem).count() == 0
    assert db_session.query(DesignProcessingJob).count() == 0
    assert db_session.query(DesignProcessingReconciliationCheck).count() == 0

    assert run(db_session, gateway).queued == 3
    assert run(db_session, gateway, now=NOW + timedelta(minutes=15)).coalesced == 3
    assert db_session.query(DesignProcessingItem).count() == 3
    assert db_session.query(DesignProcessingJob).count() == 3
    assert all(job.status == "scheduled" for job in db_session.query(DesignProcessingJob))
    assert check(db_session, first_id).last_group_id == "topics"
    assert gateway.calls[:3] == [first_id, second_id, other_active_id]
    assert sorted(gateway.calls) == sorted([first_id, second_id, other_active_id] * 3)
    assert len(requests) == 6


@pytest.mark.parametrize("outcome, reason, old_item, expected_action", [
    ("excluded", "not_registered_in_landing_zone", False, "queued"),
    ("excluded", "not_registered_in_landing_zone", True, "skipped"),
    ("excluded", "completed_folder", False, "deferred"),
    ("unavailable", "item_unavailable_to_worker", False, "deferred"),
])
def test_only_obsolete_admission_delays_are_bypassed_for_rediscovered_items(
    db_session, monkeypatch, outcome, reason, old_item, expected_action,
):
    item_id = "3249971235"
    db_session.add(DesignProcessingReconciliationCheck(
        board_id=BOARD, item_id=item_id, last_attempted_at=NOW - timedelta(minutes=5),
        last_outcome=outcome, last_reason=reason, next_check_at=NOW + timedelta(hours=6),
    ))
    db_session.commit()
    created_at = NOW - (timedelta(days=40) if old_item else timedelta(hours=2))
    monkeypatch.setattr(reconciliation, "list_items_in_groups", lambda *args: {
        "topics": [monday_client.MondayGroupItem(item_id, created_at.isoformat())],
    })
    gateway = fixtures.FakeReconciliationGateway({
        item_id: fixtures._snapshot(item_id=item_id, group_id="topics"),
    })
    result = run(db_session, gateway)
    assert result.items[0].action == expected_action
    if expected_action == "queued":
        assert gateway.calls == [item_id]
        assert check(db_session, item_id).next_check_at is None
        assert db_session.query(DesignProcessingJob).count() == 1
    else:
        assert gateway.calls == []
        assert aware(check(db_session, item_id).next_check_at) == NOW + timedelta(hours=6)
        assert db_session.query(DesignProcessingItem).count() == 0
        assert db_session.query(DesignProcessingJob).count() == 0


def test_new_active_candidate_moved_to_completed_is_excluded_before_admission(db_session, monkeypatch):
    item_id = "3249971235"
    monkeypatch.setattr(reconciliation, "list_items_in_groups", lambda *args: {
        "topics": [monday_client.MondayGroupItem(item_id, NOW.isoformat())],
    })
    gateway = fixtures.FakeReconciliationGateway({
        item_id: fixtures._snapshot(item_id=item_id, group_id="completed"),
    })
    result = run(db_session, gateway)
    assert result.excluded == 1 and result.errors == 0
    assert result.items[0].reason == "completed_folder"
    assert db_session.query(DesignProcessingItem).count() == 0
    assert db_session.query(DesignProcessingJob).count() == 0


def test_six_unavailable_items_are_recorded_and_deferred_without_changing_history(db_session, monkeypatch):
    for index, item_id in enumerate(IDS):
        item = seed(db_session, item_id, state="failed" if index == 0 else "ineligible")
        db_session.add(DesignProcessingJob(
            id=uuid.uuid4(), board_id=BOARD, item_id=item_id, trigger_type="webhook",
            status="failed" if index == 0 else "cancelled", scheduled_for=item.updated_at,
            attempt_count=0, readiness_check_count=0, max_attempts=3,
            last_error="historical error", created_at=item.updated_at, updated_at=item.updated_at,
        ))
    task = Task(
        external_task_key="test-task", account_id="account", board_id=BOARD, item_id=IDS[0],
        auto_sync_state="excluded", retention_hold=True, completed_at=NOW - timedelta(days=20),
        purge_after=NOW + timedelta(days=10),
    )
    db_session.add(task)
    db_session.commit()
    calls = []

    def request(token, query, variables, **kwargs):
        assert "exclude_nonactive: false" in query
        calls.extend(variables["itemIds"])
        return {"data": {"items": []}}

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    gateway = MondayDesignProcessingReadGateway("test", "123")
    first = run(db_session, gateway)
    assert (first.scanned, first.unavailable, first.errors) == (6, 6, 0)
    assert len(calls) == 6
    for item_id in IDS:
        recorded = check(db_session, item_id)
        assert recorded.last_reason == "item_unavailable_to_worker"
        assert recorded.last_checked_at is None
        assert aware(recorded.next_check_at) == NOW + timedelta(hours=1)
    second = run(db_session, gateway, now=NOW + timedelta(minutes=15))
    assert (second.scanned, second.deferred, second.errors) == (0, 6, 0)
    assert len(calls) == 6
    third = run(db_session, gateway, now=NOW + timedelta(hours=1))
    assert third.unavailable == 6
    assert len(calls) == 12
    assert db_session.query(DesignProcessingJob).count() == 6
    assert {job.last_error for job in db_session.query(DesignProcessingJob)} == {"historical error"}
    for item in db_session.query(DesignProcessingItem):
        assert aware(item.updated_at) == NOW - timedelta(days=40)
        assert item.extracted_parameters_json == {"Project Name": "Retained extraction"}
        assert item.state == ("failed" if item.item_id == IDS[0] else "ineligible")
    db_session.refresh(task)
    assert task.auto_sync_state == "excluded" and task.retention_hold
    assert aware(task.purge_after) == NOW + timedelta(days=10)
    assert aware(task.completed_at) == NOW - timedelta(days=20)


@pytest.mark.parametrize("forced", [False, True])
def test_available_again_resumes_and_clears_delay(db_session, forced):
    seed(db_session)
    class MissingGateway:
        def fetch_target(self, item_id):
            raise monday_client.MondayItemUnavailable()

    assert run(db_session, MissingGateway()).unavailable == 1
    restored = fixtures.FakeReconciliationGateway({IDS[0]: fixtures._snapshot(item_id=IDS[0], group_id="topics")})
    args = dict(now=NOW + timedelta(minutes=10), item_id=IDS[0]) if forced else dict(now=NOW + timedelta(hours=1))
    result = run(db_session, restored, **args)
    assert result.queued == 1 and result.errors == 0
    recorded = check(db_session)
    assert recorded.next_check_at is None
    assert recorded.last_outcome == "queued" and recorded.last_group_id == "topics"
    assert aware(recorded.last_checked_at) == args["now"]


def test_unregistered_missing_item_observation_does_not_bypass_broad_activation_boundary(db_session, monkeypatch):
    class MissingGateway:
        def fetch_target(self, item_id):
            raise monday_client.MondayItemUnavailable()

    assert run(db_session, MissingGateway(), item_id=IDS[0]).unavailable == 1
    assert db_session.query(DesignProcessingItem).count() == 0
    monkeypatch.setattr(reconciliation, "list_items_in_groups", lambda *args, **kwargs: {
        "topics": [monday_client.MondayGroupItem(IDS[0], (NOW - timedelta(days=40)).isoformat())],
    })
    gateway = fixtures.FakeReconciliationGateway({IDS[0]: fixtures._snapshot(item_id=IDS[0], group_id="topics")})
    result = run(db_session, gateway, now=NOW + timedelta(hours=1))
    assert result.scanned == 0 and gateway.calls == []
    assert result.items[0].reason == "before_activation_timestamp"
    assert db_session.query(DesignProcessingItem).count() == 0
    assert db_session.query(DesignProcessingJob).count() == 0
    # Explicit operator admission can still bypass the broad-scan boundary.
    result = run(db_session, gateway, now=NOW + timedelta(hours=1), item_id=IDS[0])
    assert result.queued == 1
    assert db_session.query(DesignProcessingItem).count() == 1
    assert db_session.query(DesignProcessingJob).count() == 1


def test_completed_items_are_deferred_then_recover_if_moved_back(db_session):
    seed(db_session)
    completed = fixtures.FakeReconciliationGateway({IDS[0]: fixtures._snapshot(item_id=IDS[0], group_id="completed")})
    result = run(db_session, completed)
    assert result.items[0].reason == "completed_folder"
    assert result.items[0].group_id == "completed"
    assert result.items[0].next_check_at == NOW + timedelta(hours=6)
    assert run(db_session, completed, now=NOW + timedelta(hours=1)).deferred == 1
    assert completed.calls == [IDS[0]]
    active = fixtures.FakeReconciliationGateway({IDS[0]: fixtures._snapshot(item_id=IDS[0], group_id="topics")})
    assert run(db_session, active, now=NOW + timedelta(hours=6)).queued == 1
    assert check(db_session).next_check_at is None


def test_webhook_processing_is_not_blocked_by_reconciliation_delay(db_session):
    seed(db_session)
    completed = fixtures.FakeReconciliationGateway({IDS[0]: fixtures._snapshot(item_id=IDS[0], group_id="completed")})
    run(db_session, completed)
    result = queue_design_processing_snapshot(
        db_session, fixtures._snapshot(item_id=IDS[0], group_id="topics"),
        trigger_type="webhook", mode="enabled", pipeline_version=settings.design_processing_pipeline_version,
        expected_board_id=BOARD, expected_group_id=LANDING, now=NOW + timedelta(minutes=1),
    )
    db_session.commit()
    assert result.outcome == "queued"


def test_deferred_items_do_not_take_limited_scan_slots(db_session, monkeypatch):
    seed(db_session, "1")
    completed = fixtures.FakeReconciliationGateway({"1": fixtures._snapshot(item_id="1", group_id="completed")})
    run(db_session, completed)
    # Even an item present in the Landing listing must respect its recorded
    # lookup delay; a listing alone cannot bypass the per-item availability gate.
    monkeypatch.setattr(reconciliation, "list_items_in_groups", lambda *args, **kwargs: {
        LANDING: [monday_client.MondayGroupItem("1", NOW.isoformat())],
    })
    seed(db_session, "2")
    gateway = fixtures.FakeReconciliationGateway({"2": fixtures._snapshot(item_id="2")})
    result = run(db_session, gateway, limit=1, now=NOW + timedelta(minutes=15))
    assert (result.scanned, result.deferred, result.queued) == (1, 1, 1)
    assert gateway.calls == ["2"]


def test_unavailable_preserves_last_successful_observation(db_session):
    seed(db_session)
    completed = fixtures.FakeReconciliationGateway({IDS[0]: fixtures._snapshot(item_id=IDS[0], group_id="completed")})
    run(db_session, completed)

    class MissingGateway:
        def fetch_target(self, item_id):
            raise monday_client.MondayItemUnavailable()

    run(db_session, MissingGateway(), item_id=IDS[0], now=NOW + timedelta(minutes=5))
    recorded = check(db_session)
    assert recorded.last_group_id == "completed"
    assert aware(recorded.last_checked_at) == NOW
    assert aware(recorded.last_attempted_at) == NOW + timedelta(minutes=5)
    assert recorded.last_outcome == "unavailable"


def test_database_failure_is_an_error_and_rolls_back_queued_work(db_session, monkeypatch):
    seed(db_session)
    def fail_record(*args, **kwargs):
        raise SQLAlchemyError("database write failed")

    monkeypatch.setattr(reconciliation, "_record_check", fail_record)
    gateway = fixtures.FakeReconciliationGateway({IDS[0]: fixtures._snapshot(item_id=IDS[0])})
    result = run(db_session, gateway)
    assert result.errors == 1 and result.queued == 0
    assert db_session.query(DesignProcessingJob).count() == 0
    assert db_session.query(DesignProcessingItem).one().state == "ineligible"


@pytest.mark.parametrize("error", [
    HTTPException(401, "authentication failed"), HTTPException(403, "access denied"),
    HTTPException(404, "some other endpoint missing"), HTTPException(502, "GraphQL failed"),
    monday_client.TransientMondayAPIError(429, retry_after_seconds=60),
    monday_client.MondayReadContractError(detail="malformed response"),
    TimeoutError("connection timed out"),
])
def test_real_failures_remain_errors_and_do_not_starve_later_items(db_session, error):
    seed(db_session, "1", age_days=50)
    seed(db_session, "2", age_days=40)

    class Gateway:
        def fetch_target(self, item_id):
            if item_id == "1":
                raise error
            return fixtures._snapshot(item_id=item_id)

    first = run(db_session, Gateway(), limit=1)
    assert first.errors == 1 and first.unavailable == 0
    recorded = check(db_session, "1")
    assert recorded.last_outcome == "error" and recorded.next_check_at is None
    second = run(db_session, Gateway(), limit=1, now=NOW + timedelta(minutes=15))
    assert second.queued == 1 and second.errors == 0
    assert second.items[0].item_id == "2"


@pytest.mark.parametrize("outcome", ["unavailable", "excluded", "success", "error"])
def test_dry_run_changes_neither_checks_nor_items(db_session, outcome):
    item = seed(db_session)
    original_updated = item.updated_at
    class Gateway:
        def fetch_target(self, item_id):
            if outcome == "unavailable":
                raise monday_client.MondayItemUnavailable()
            if outcome == "error":
                raise HTTPException(502, "upstream failure")
            return fixtures._snapshot(item_id=item_id, group_id="completed" if outcome == "excluded" else LANDING)

    result = run(db_session, Gateway(), dry_run=True, item_id=IDS[0])
    assert result.scanned == 1
    db_session.commit()
    db_session.refresh(item)
    assert item.state == "ineligible" and item.updated_at == original_updated
    assert db_session.query(DesignProcessingReconciliationCheck).count() == 0
    assert db_session.query(DesignProcessingJob).count() == 0


def test_explicit_dry_run_does_not_clear_existing_delay(db_session):
    seed(db_session)
    completed = fixtures.FakeReconciliationGateway({IDS[0]: fixtures._snapshot(item_id=IDS[0], group_id="completed")})
    run(db_session, completed)
    active = fixtures.FakeReconciliationGateway({IDS[0]: fixtures._snapshot(item_id=IDS[0], group_id="topics")})
    assert run(db_session, active, item_id=IDS[0], now=NOW + timedelta(minutes=5), dry_run=True).queued == 1
    recorded = check(db_session)
    assert recorded.last_reason == "completed_folder"
    assert aware(recorded.next_check_at) == NOW + timedelta(hours=6)
    assert db_session.query(DesignProcessingJob).count() == 0


def test_validated_empty_response_is_distinct_from_malformed_response(monkeypatch):
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs: {"data": {"items": []}})
    with pytest.raises(monday_client.MondayItemUnavailable):
        monday_client.fetch_design_processing_intake_item("test", IDS[0])
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs: {"data": {"items": None}})
    with pytest.raises(monday_client.MondayReadContractError):
        monday_client.fetch_design_processing_intake_item("test", IDS[0])


@pytest.mark.parametrize("errors, unavailable, expected_exit", [(0, 6, 0), (1, 0, 1)])
def test_cron_exit_and_compact_summary(monkeypatch, capsys, errors, unavailable, expected_exit):
    monkeypatch.setattr("sys.argv", ["reconciliation"])
    monkeypatch.setattr(reconciliation, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(reconciliation, "reconcile_landing_zone_once", lambda *args, **kwargs:
        reconciliation.DesignProcessingReconciliationResult(
            dry_run=False, board_id=BOARD, mode="enabled", errors=errors, unavailable=unavailable,
        ))
    assert reconciliation.main() == expected_exit
    payload = json.loads(capsys.readouterr().out)
    assert payload["unavailable"] == unavailable and payload["errors"] == errors
    assert "items" not in payload


def test_migration_round_trip_preserves_existing_records(monkeypatch):
    migration = importlib.import_module("backend.migrations.versions.0016_design_reconciliation")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE design_processing_items (item_id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO design_processing_items VALUES ('retained')"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        columns = {column["name"] for column in inspect(connection).get_columns("design_processing_reconciliation_checks")}
        assert columns == set(DesignProcessingReconciliationCheck.__table__.columns.keys())
        connection.execute(DesignProcessingReconciliationCheck.__table__.insert().values(
            board_id=BOARD, item_id=IDS[0], last_attempted_at=NOW, last_outcome="unavailable",
            last_reason="item_unavailable_to_worker", next_check_at=NOW + timedelta(hours=1),
        ))
        migration.downgrade()
        assert connection.execute(text("SELECT item_id FROM design_processing_items")).scalar_one() == "retained"
        assert "design_processing_reconciliation_checks" not in inspect(connection).get_table_names()
    engine.dispose()
