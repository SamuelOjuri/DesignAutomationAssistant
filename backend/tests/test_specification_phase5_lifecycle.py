"""Landing Zone publication survives the independent ingestion lifecycle."""

from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest

from backend.app.config import settings
from backend.app.models import (
    DesignProcessingReconciliationCheck, MondayWebhookEvent, PerformanceSpecification,
    PerformanceSpecificationLink, Task, TaskMondayMetadata,
)
from backend.app.routes import monday_webhooks
from backend.app.services import design_processing_reconciliation as reconciliation
from backend.app.services import monday_metadata, performance_specification as worker
from backend.app.services.auto_sync import apply_auto_sync_policy_for_item
from backend.app.services.design_processing_inputs import DesignEmailAsset, DesignProcessingTargetSnapshot
from backend.app.monday_client import MondayGroupItem
from backend.tests.test_performance_specification import BOARD, ITEM, case, current


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(settings, "auto_sync_enabled", True)
    monkeypatch.setattr(settings, "auto_sync_board_id", BOARD)
    monkeypatch.setattr(settings, "design_processing_board_id", BOARD)
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")


def item(group=None, state="active"):
    return {
        "id": ITEM, "account_id": "acct", "name": "A client request", "state": state,
        "board": {"id": BOARD}, "group": {"id": group or settings.design_processing_landing_group_id},
        "assets": [], "updates": [], "column_values": [],
    }


def task(case):
    value = Task(external_task_key=f"acct:{BOARD}:{ITEM}", account_id="acct", board_id=BOARD,
        item_id=ITEM, source_group_id=settings.design_processing_landing_group_id,
        auto_sync_state="active", auto_sync_enabled=True, sync_status="completed")
    case.db.add(value)
    case.db.commit()
    return value


def dispatch(case, payload, *, live_item=None):
    event = MondayWebhookEvent(id=uuid.uuid4(), idempotency_key=uuid.uuid4().hex,
        status="processing", payload_json=payload)
    case.db.add(event)
    case.db.commit()
    value = SimpleNamespace(webhook_event_id=event.id)
    monday_webhooks._dispatch_auto_sync(case.db, value, item=live_item,
        normalized=monday_webhooks.normalize_webhook_payload(payload))
    case.db.commit()
    return value


def test_landing_webhook_followed_by_ingestion_exclusion_keeps_publication_pending(case):
    value = task(case)
    monday_metadata.enqueue_metadata(case.db, value, immediate=True)
    case.db.commit()
    result = dispatch(case, {"event": {"boardId": BOARD, "pulseId": ITEM,
        "type": "change_column_value", "columnId": "file_mkpbm883"}}, live_item=item())
    assert result.outcome == "excluded"
    assert value.auto_sync_state == "excluded"
    metadata = case.db.get(TaskMondayMetadata, value.external_task_key)
    assert metadata.scheduled_for is None
    specification = current(case)
    assert specification.scheduled_for is not None
    assert specification.requested_generation > specification.completed_generation


@pytest.mark.parametrize("group,state,cancelled", [
    (None, "active", False), ("topics", "active", True), ("completed", "active", True),
    (None, "archived", True), (None, "deleted", True),
])
def test_live_scope_cancellation_is_independent_of_metadata(case, group, state, cancelled):
    value = task(case)
    specification = worker.enqueue_specification(case.db, BOARD, ITEM,
        group_id=settings.design_processing_landing_group_id, immediate=True)
    specification.current_revision = "retained-pdf"
    specification.lease_token = "old-lease"
    case.db.commit()
    apply_auto_sync_policy_for_item(case.db, item(group, state), trigger_type="reconciliation")
    case.db.commit()
    specification = current(case)
    assert (specification.scheduled_for is None) is cancelled
    assert specification.current_revision == "retained-pdf"
    if cancelled:
        assert specification.lease_token is None
        assert specification.completed_generation == specification.requested_generation


@pytest.mark.parametrize("column", ["date_mkpb23av", "board_relation_mm3c4g5x",
    "file_mkza7y37", "file_mm59rntf"])
def test_holding_display_fields_and_generated_outputs_do_not_wake_specification(case, column):
    worker.enqueue_specification(case.db, BOARD, ITEM,
        group_id=settings.design_processing_landing_group_id, immediate=True)
    case.db.commit()
    before = current(case).requested_generation
    dispatch(case, {"event": {"boardId": BOARD, "pulseId": ITEM,
        "type": "change_column_value", "columnId": column}}, live_item=item())
    assert current(case).requested_generation == before


@pytest.mark.parametrize("event_type", ["change_column_value", "item_archived", "item_deleted", "item_restored"])
def test_linked_source_field_and_lifecycle_events_queue_review_without_task(case, event_type):
    worker.enqueue_specification(case.db, BOARD, ITEM,
        group_id=settings.design_processing_landing_group_id, immediate=True)
    case.db.add(PerformanceSpecificationLink(board_id=BOARD, item_id=ITEM,
        linked_board_id="source-board", linked_item_id="contact-id"))
    case.db.commit()
    before = current(case).requested_generation
    dispatch(case, {"event": {"boardId": "source-board", "pulseId": "contact-id",
        "type": event_type, "columnId": "contact-field"}})
    assert current(case).requested_generation == before + 1
    assert case.db.query(Task).count() == 0


@pytest.mark.parametrize("event_type", ["create_subitem", "change_subitem_column_value", "change_subitem_name",
    "move_subitem", "subitem_archived", "subitem_deleted"])
def test_drawing_and_parent_dependency_notifications_queue_each_specification_once(case, event_type):
    worker.enqueue_specification(case.db, BOARD, ITEM,
        group_id=settings.design_processing_landing_group_id, immediate=True)
    case.db.add_all([
        PerformanceSpecificationLink(board_id=BOARD, item_id=ITEM,
            linked_board_id="drawing-board", linked_item_id="drawing-id"),
        PerformanceSpecificationLink(board_id=BOARD, item_id=ITEM,
            linked_board_id=str(settings.design_processing_project_board_id), linked_item_id="project-id"),
    ])
    case.db.commit()
    before = current(case).requested_generation
    dispatch(case, {"event": {"boardId": "drawing-board", "pulseId": "drawing-id",
        "parentItemId": "project-id", "type": event_type, "columnId": "u-value"}})
    assert current(case).requested_generation == before + 1


def test_review_holding_board_requires_live_scope_without_active_ingestion(monkeypatch):
    monkeypatch.setattr(settings, "performance_specification_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_mode", "shadow")
    monkeypatch.setattr(settings, "design_processing_mode", "off")
    monkeypatch.setattr(settings, "auto_sync_board_id", "a-different-board")
    event = monday_webhooks.normalize_webhook_payload({"event": {
        "boardId": BOARD, "pulseId": ITEM, "type": "item_moved_to_any_group",
    }})
    assert monday_webhooks._dispatch_requires_snapshot("auto_sync", event)
    assert monday_webhooks._design_trigger_type(event) == "webhook_move"


def test_registered_settled_landing_item_recovers_cancelled_specification_before_long_design_deadline(case, monkeypatch):
    now = worker.utc_now()
    version = settings.design_processing_pipeline_version
    case.item.latest_published_input_revision = "email-v1"
    case.item.latest_published_pipeline_version = version
    case.item.created_at = case.item.updated_at = now - timedelta(days=30)
    worker.enqueue_specification(case.db, BOARD, ITEM,
        group_id=settings.design_processing_landing_group_id, immediate=True)
    worker.cancel_specification(case.db, BOARD, ITEM)
    case.db.add(DesignProcessingReconciliationCheck(
        board_id=BOARD, item_id=ITEM, last_attempted_at=now - timedelta(minutes=1),
        last_checked_at=now - timedelta(minutes=1), last_outcome="ignored", last_reason="ready",
        last_group_id=settings.design_processing_landing_group_id,
        next_check_at=now + timedelta(hours=12),
    ))
    case.db.commit()
    monkeypatch.setattr(reconciliation, "list_items_in_groups", lambda *args: {
        settings.design_processing_landing_group_id: [MondayGroupItem(ITEM, (now - timedelta(days=30)).isoformat())],
    })
    snapshot = DesignProcessingTargetSnapshot(board_id=BOARD, item_id=ITEM,
        group_id=settings.design_processing_landing_group_id, item_state="active", name="Project",
        input_revision="email-v1", email_assets=(DesignEmailAsset(asset_id="1", filename="request.eml",
            file_extension="eml", size=1, created_at=now.isoformat(), download_url="unused",
            download_requires_auth=False),))
    result = reconciliation.reconcile_landing_zone_once(case.db, dry_run=False, access_token="test",
        mode="enabled", now=now, activation_timestamp=now - timedelta(days=1),
        gateway=SimpleNamespace(fetch_target=lambda _: snapshot))
    assert result.scanned == 1 and result.deferred == 0 and result.errors == 0
    assert current(case).scheduled_for is not None
    assert case.db.query(Task).count() == 0
