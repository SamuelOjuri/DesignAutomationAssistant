"""Shared availability suppresses duplicate reads without discarding outputs."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

from starlette.requests import Request
import pytest

from backend.app.config import settings
from backend.app.models import (
    AutoSyncJob, AutoSyncReconciliationCheck, DesignProcessingItem, DesignProcessingJob,
    MondayItemAvailability, PerformanceSpecification, PerformanceSpecificationArtifact, Task, TaskMondayMetadata,
)
from backend.app.monday_client import MondayItemUnavailable
from backend.app.routes import monday_webhooks as webhooks
from backend.app.services import auto_sync_reconciliation as reconciliation
from backend.app.services.auto_sync import apply_auto_sync_policy_for_item
from backend.app.services.monday_item_availability import availability_decision, observe_item, observe_unavailable, record_retired
from backend.tests.test_monday_metadata import case

BOARD = "1882196103"
NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


@pytest.fixture
def configured(case, monkeypatch):
    monkeypatch.setattr(settings, "monday_ingestion_access_token", "token")
    monkeypatch.setattr(settings, "monday_item_availability_retry_seconds", [3600, 21600])
    monkeypatch.setattr(settings, "auto_sync_reconciliation_lifecycle_interval_seconds", 0)
    monkeypatch.setattr(settings, "monday_webhook_shared_secret", "test-secret")
    monkeypatch.setattr(settings, "design_processing_mode", "off")
    monkeypatch.setattr(settings, "performance_specification_enabled", False)
    monkeypatch.setattr(reconciliation, "utc_now", lambda: NOW)
    monkeypatch.setattr(webhooks, "utc_now", lambda: NOW)
    monkeypatch.setattr(reconciliation, "fetch_current_account_id", lambda token: "acct")
    monkeypatch.setattr(reconciliation, "list_item_ids_in_groups", lambda *a, **kw: {"topics": ["123"]})
    monkeypatch.setattr(webhooks, "get_monday_ingestion_access_token", lambda: "token")
    return case


def active(case, **kwargs):
    return reconciliation.reconcile_active_items_once(case.db, access_token="token", dry_run=False, **kwargs)


def lifecycle(case, **kwargs):
    return reconciliation.detect_completed_transitions_once(case.db, access_token="token", dry_run=False, **kwargs)


def test_validated_active_miss_is_shared_with_lifecycle_and_forced_sweeps(configured, monkeypatch):
    reads = []
    def missing(*args, **kwargs):
        reads.append(args)
        raise MondayItemUnavailable()
    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", missing)
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *a, **k: pytest.fail("Shared backoff was ignored"))
    assert active(configured).source_unavailable == 1
    assert active(configured, force=True).scanned == 0
    assert lifecycle(configured, force=True).scanned == 0
    assert len(reads) == 1
    assert configured.task.auto_sync_state == "active"  # Missing visibility is not deletion.
    row = configured.db.query(MondayItemAvailability).one()
    assert row.state == "unavailable" and row.consecutive_misses == 1


def test_lifecycle_miss_prevents_the_next_active_source_read(configured, monkeypatch):
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *a, **k: (_ for _ in ()).throw(MondayItemUnavailable()))
    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", lambda *a, **k: pytest.fail("Repeated source read"))
    assert lifecycle(configured).source_unavailable == 1
    assert active(configured).scanned == 0


def tracked_outputs(case):
    apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook", desired_source_revision="first")
    item = DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id="123", state="scheduled", warnings_json=[])
    case.db.add(item)
    case.db.flush()
    case.db.add(DesignProcessingJob(id=uuid.uuid4(), board_id=BOARD, item_id="123", trigger_type="webhook_create",
        status="scheduled", scheduled_for=NOW, attempt_count=0, readiness_check_count=0, max_attempts=3, created_at=NOW, updated_at=NOW))
    spec = PerformanceSpecification(board_id=BOARD, item_id="123", requested_generation=3, completed_generation=1,
        scheduled_for=NOW, current_revision="retained", attempt_count=0)
    case.db.add(spec)
    case.db.flush()
    case.db.add(PerformanceSpecificationArtifact(board_id=BOARD, item_id="123", revision="retained", translation_json={},
        model_version="retained", filename="retained.pdf", storage_bucket="private", storage_object_key="retained.pdf",
        content_sha256="retained", size_bytes=123, monday_asset_id="asset-1", status="published"))
    case.db.commit()


@pytest.mark.parametrize("entry", ["active", "lifecycle"])
def test_verified_cross_board_move_cancels_old_registration_and_retains_files(configured, monkeypatch, entry):
    tracked_outputs(configured)
    moved = {**deepcopy(configured.item), "board": {"id": "999"}}
    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", lambda *a, **k: moved)
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *a, **k: moved)
    result = active(configured) if entry == "active" else lifecycle(configured)
    assert result.errors == 0 and result.items[0].action == "excluded"
    assert result.items[0].reason == "board_not_managed"
    assert configured.db.get(Task, configured.task.external_task_key).auto_sync_state == "excluded"
    assert {job.status for job in configured.db.query(AutoSyncJob)} == {"cancelled"}
    assert configured.db.query(DesignProcessingJob).one().status == "cancelled"
    assert configured.db.query(DesignProcessingItem).one().state == "ineligible"
    assert configured.db.get(TaskMondayMetadata, configured.task.external_task_key).scheduled_for is None
    spec = configured.db.get(PerformanceSpecification, (BOARD, "123"))
    assert spec.scheduled_for is None and spec.current_revision == "retained"
    assert configured.db.get(PerformanceSpecificationArtifact, (BOARD, "123", "retained")).monday_asset_id == "asset-1"
    assert configured.db.query(Task).count() == 1  # No new-board registration.
    assert not availability_decision(configured.db, BOARD, "123", access_token="token", now=NOW).allowed
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *a, **k: pytest.fail("Moved item was immediately re-read"))
    assert lifecycle(configured, force=True).scanned == 0


def test_explicit_positive_return_to_original_board_can_reactivate_excluded_task(configured, monkeypatch):
    moved = {**deepcopy(configured.item), "board": {"id": "999"}}
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *a, **k: moved)
    assert lifecycle(configured).items[0].action == "excluded"
    monkeypatch.setattr(reconciliation, "utc_now", lambda: NOW + timedelta(seconds=1))
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *a, **k: deepcopy(configured.item))
    result = lifecycle(configured, item_id="123")
    assert result.errors == 0 and result.reactivated == 1
    assert configured.task.auto_sync_state == "active" and configured.task.auto_sync_enabled


@pytest.mark.parametrize("newer", ["archived", "unavailable"])
def test_delayed_source_response_cannot_undo_a_newer_shared_observation(configured, monkeypatch, newer):
    def delayed(*a, **k):
        if newer == "archived":
            observe_item(configured.db, BOARD, {**configured.item, "state": "archived"}, access_token="token", observed_at=NOW + timedelta(seconds=1))
        else:
            observe_unavailable(configured.db, BOARD, "123", access_token="token", observed_at=NOW + timedelta(seconds=1))
        configured.db.commit()
        return deepcopy(configured.item)
    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", delayed)
    assert active(configured).queued == 0
    assert configured.db.query(TaskMondayMetadata).count() == 0
    assert configured.db.query(MondayItemAvailability).one().state == newer


def test_old_out_of_board_read_cannot_cancel_a_newer_restored_registration(configured, monkeypatch):
    tracked_outputs(configured)
    def delayed(*a, **k):
        observe_item(configured.db, BOARD, configured.item, access_token="token", observed_at=NOW + timedelta(seconds=1))
        configured.db.commit()
        return {**deepcopy(configured.item), "board": {"id": "999"}}
    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", delayed)
    assert active(configured).errors == 0
    assert configured.task.auto_sync_state == "active"
    assert configured.db.query(DesignProcessingJob).one().status == "scheduled"
    assert configured.db.get(PerformanceSpecification, (BOARD, "123")).scheduled_for is not None


@pytest.mark.parametrize("state,board", [("active", BOARD), ("archived", BOARD), ("active", "999")])
def test_production_lifecycle_batch_status_avoids_individual_metadata_reads(configured, monkeypatch, state, board):
    from backend.app.services import monday_item_probes
    reads = []
    def individual(*a, **k):
        pytest.fail("Batched lifecycle status caused an individual read")
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", individual)
    monkeypatch.setattr(reconciliation, "_DEFAULT_LIFECYCLE_READER", individual)
    def probe(db, board_id, item_ids, **kwargs):
        reads.append((board_id, item_ids))
        status = {**deepcopy(configured.item), "state": state, "board": {"id": board}}
        observe_item(db, board_id, status, access_token="token", observed_at=NOW)
        db.commit()
        return {"123": status}
    monkeypatch.setattr(monday_item_probes, "probe_items", probe)
    result = lifecycle(configured)
    assert result.errors == 0 and reads == [(BOARD, ["123"])]
    assert result.items[0].action == ("still_active" if state == "active" and board == BOARD else "archived" if state == "archived" else "excluded")


@pytest.mark.parametrize("old_status", ["archived", "out_of_board", "unavailable"])
def test_batch_result_cannot_override_a_restore_arriving_before_consumer_commit(configured, monkeypatch, old_status):
    from backend.app.services import monday_item_probes
    tracked_outputs(configured)
    def individual(*a, **k):
        pytest.fail("No individual read should follow this stale batch result")
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", individual)
    monkeypatch.setattr(reconciliation, "_DEFAULT_LIFECYCLE_READER", individual)
    def probe(db, board_id, item_ids, **kwargs):
        old = None if old_status == "unavailable" else {**deepcopy(configured.item),
            "state": "archived" if old_status == "archived" else "active", "board": {"id": "999" if old_status == "out_of_board" else BOARD}}
        if old is None:
            observe_unavailable(db, board_id, "123", access_token="token", observed_at=NOW)
        else:
            observe_item(db, board_id, old, access_token="token", observed_at=NOW)
        observe_item(db, board_id, configured.item, access_token="token", observed_at=NOW + timedelta(seconds=1))
        db.commit()
        return {"123": old}
    monkeypatch.setattr(monday_item_probes, "probe_items", probe)
    assert lifecycle(configured).errors == 0
    assert configured.task.auto_sync_state == "active"
    assert configured.db.query(DesignProcessingJob).one().status == "scheduled"
    assert configured.db.get(PerformanceSpecification, (BOARD, "123")).scheduled_for is not None
    assert configured.db.query(AutoSyncReconciliationCheck).count() == 0


@pytest.fixture
def client(configured):
    # Invoke the real authenticated route in the fixture's thread. The shared
    # metadata fixture intentionally uses a thread-local SQLite connection.
    return configured.db


def send(client, kind="change_column_value", *, trigger=None, when=None):
    event = {"boardId": BOARD, "pulseId": "123", "groupId": "topics", "type": kind,
        "columnId": "files", "triggerUuid": trigger or uuid.uuid4().hex}
    if when is not None:
        event["triggerTime"] = when.isoformat()
    request = Request({"type": "http", "scheme": "http", "method": "POST", "server": ("testserver", 80),
        "client": ("testclient", 123), "path": "/api/monday/webhooks", "query_string": b"", "headers": []})
    body = webhooks.monday_webhook({"event": event}, request, authorization=None, shared_secret_token="test-secret", db=client)
    return SimpleNamespace(status_code=200, json=lambda: body)


def test_webhook_missing_is_acknowledged_without_deletion_or_repeated_reads(configured, client, monkeypatch):
    calls = []
    def missing(*args):
        calls.append(args)
        raise MondayItemUnavailable()
    monkeypatch.setattr(webhooks, "fetch_current_source_revision_inputs", missing)
    assert send(client).status_code == 200
    assert send(client).status_code == 200
    assert len(calls) == 1
    assert configured.db.query(MondayItemAvailability).one().state == "unavailable"
    assert configured.task.auto_sync_state == "active"


def test_delayed_lifecycle_event_does_not_undo_newer_restore_or_force_a_read(configured, client, monkeypatch):
    observe_item(configured.db, BOARD, configured.item, access_token="token", observed_at=NOW)
    configured.db.commit()
    monkeypatch.setattr(webhooks, "fetch_current_source_revision_inputs", lambda *a: pytest.fail("Delayed notification read Monday"))
    response = send(client, "item_deleted", when=NOW - timedelta(minutes=1))
    assert response.status_code == 200 and response.json()["status"] == "ignored"
    assert configured.db.query(MondayItemAvailability).one().state == "available"
    assert configured.task.auto_sync_state == "active"


def test_authenticated_restore_notification_only_reopens_after_a_positive_live_read(configured, client, monkeypatch):
    observe_item(configured.db, BOARD, {**configured.item, "state": "archived"}, access_token="token", observed_at=NOW - timedelta(minutes=1))
    configured.db.commit()
    monkeypatch.setattr(webhooks, "fetch_current_source_revision_inputs", lambda *a: deepcopy(configured.item))
    response = send(client, "item_restored", when=NOW - timedelta(seconds=1))
    assert response.status_code == 200
    assert configured.db.query(MondayItemAvailability).one().state == "available"
    assert configured.task.auto_sync_state == "active"


def test_confirmed_retirement_still_suppresses_generic_and_delayed_lifecycle_webhooks(configured, client, monkeypatch):
    record_retired(configured.db, BOARD, "123", access_token="token", observed_at=NOW - timedelta(days=1))
    configured.db.commit()
    monkeypatch.setattr(webhooks, "fetch_current_source_revision_inputs", lambda *a: pytest.fail("Retired source was read"))
    assert send(client).status_code == 200
    assert send(client, "item_restored", when=NOW - timedelta(days=2)).status_code == 200
    assert configured.db.query(MondayItemAvailability).one().state == "retired"


@pytest.mark.parametrize("visible", [True, False])
def test_current_authenticated_restore_rechecks_shared_and_legacy_retirement_without_inventing_state(configured, client, monkeypatch, visible):
    retired_at = NOW - timedelta(days=1)
    record_retired(configured.db, BOARD, "123", access_token="token", observed_at=retired_at)
    configured.db.add(AutoSyncReconciliationCheck(board_id=BOARD, item_id="123", scope="completed_transition",
        last_attempted_at=retired_at, last_checked_at=retired_at, last_outcome="retired", last_reason="confirmed_deleted"))
    configured.task.auto_sync_state, configured.task.auto_sync_enabled = "deleted", False
    configured.db.commit()
    calls = []
    def live(*args):
        calls.append(args)
        if not visible:
            raise MondayItemUnavailable()
        return deepcopy(configured.item)
    monkeypatch.setattr(webhooks, "fetch_current_source_revision_inputs", live)
    response = send(client, "item_restored", when=NOW - timedelta(seconds=1))
    assert response.status_code == 200 and len(calls) == 1
    row = configured.db.query(MondayItemAvailability).one()
    assert row.state == ("available" if visible else "retired")
    assert configured.task.auto_sync_state == ("active" if visible else "deleted")


def test_active_batch_filter_does_not_let_suppressed_first_item_starve_next(configured, monkeypatch):
    observe_unavailable(configured.db, BOARD, "123", access_token="token", observed_at=NOW)
    configured.db.commit()
    monkeypatch.setattr(reconciliation, "list_item_ids_in_groups", lambda *a, **k: {"topics": ["123", "124"]})
    seen = []
    def fetch(token, identity, **kwargs):
        seen.append(identity)
        return {**deepcopy(configured.item), "id": identity}
    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", fetch)
    result = active(configured, limit=1)
    assert result.scanned == 1 and seen == ["124"]
