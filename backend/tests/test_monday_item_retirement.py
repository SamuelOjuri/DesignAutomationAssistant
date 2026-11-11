"""Operator retirement of tracked items confirmed deleted and purged on Monday."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import HTTPException

from backend.app.config import settings
from backend.app.models import (
    AutoSyncJob, AutoSyncReconciliationCheck, DesignProcessingItem, DesignProcessingJob,
    DesignProcessingReconciliationCheck, PerformanceSpecification, PerformanceSpecificationArtifact,
    PerformanceSpecificationReview, PerformanceSpecificationReviewEvent, Task,
)
from backend.app.monday_client import MondayItemUnavailable
from backend.app.services import auto_sync_reconciliation as reconciliation
from backend.app.services.auto_sync import apply_auto_sync_policy_for_item
from backend.app.services.auto_sync_policy import ACTIVE_JOB_STATUSES
from backend.app.services.design_processing_reconciliation import reconcile_landing_zone_once
from backend.app.services.monday_item_retirement import retire_deleted_items
from backend.app.services.monday_item_availability import observe_item
from backend.tests.test_monday_metadata import case

BOARD = "1882196103"
NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)


def purged(item_id):
    raise MondayItemUnavailable()


def lifecycle(case, **kwargs):
    return reconciliation.detect_completed_transitions_once(case.db, access_token="token", dry_run=False, **kwargs)


def design_sweep(case, **kwargs):
    def fetch_target(item_id):
        raise MondayItemUnavailable()
    return reconcile_landing_zone_once(
        case.db, dry_run=False, access_token="token", mode="enabled", now=NOW,
        gateway=type("Gateway", (), {"fetch_target": staticmethod(fetch_target)})(), **kwargs,
    )


@pytest.fixture()
def tracked(case, monkeypatch):
    monkeypatch.setattr(settings, "performance_specification_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_mode", "enabled")
    monkeypatch.setattr(settings, "design_processing_board_id", BOARD)
    monkeypatch.setattr(settings, "auto_sync_reconciliation_unavailable_interval_seconds", 3600)
    apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook", desired_source_revision="rev-1")
    item = DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id="123", state="scheduled", warnings_json=[])
    case.db.add(item)
    case.db.flush()
    case.db.add(DesignProcessingJob(
        id=uuid.uuid4(), board_id=BOARD, item_id="123", trigger_type="webhook_create", status="scheduled",
        scheduled_for=NOW, attempt_count=0, readiness_check_count=0, max_attempts=3, created_at=NOW, updated_at=NOW,
    ))
    case.db.add(PerformanceSpecification(board_id=BOARD, item_id="123", requested_generation=5,
                                         completed_generation=1, attempt_count=4, scheduled_for=NOW))
    case.db.commit()
    assert case.db.query(AutoSyncJob).filter(
        AutoSyncJob.item_id == "123", AutoSyncJob.status.in_(ACTIVE_JOB_STATUSES)).count() == 1
    return item


def test_purged_item_is_retired_like_a_live_delete_and_scheduled_checks_stop(case, tracked, monkeypatch):
    [result] = retire_deleted_items(case.db, ["123"], fetch_item=purged, dry_run=False)
    assert (result.action, result.tasks, result.design_state) == ("retired", 1, "ineligible")

    task = case.db.get(Task, case.task.external_task_key, populate_existing=True)
    assert task.auto_sync_state == "deleted" and not task.auto_sync_enabled
    assert task.source_group_id == "topics" and task.purge_after is None
    assert {job.status for job in case.db.query(AutoSyncJob).filter_by(item_id="123")} == {"cancelled"}
    assert case.db.query(DesignProcessingJob).filter_by(item_id="123").one().status == "cancelled"
    assert case.db.get(DesignProcessingItem, tracked.id, populate_existing=True).state == "ineligible"
    spec = case.db.get(PerformanceSpecification, (BOARD, "123"), populate_existing=True)
    assert spec.completed_generation == spec.requested_generation and spec.scheduled_for is None
    lifecycle_check = case.db.get(AutoSyncReconciliationCheck, (BOARD, "123", "completed_transition"))
    design_check = case.db.get(DesignProcessingReconciliationCheck, (BOARD, "123"))
    assert (lifecycle_check.last_outcome, lifecycle_check.last_reason) == ("retired", "confirmed_deleted")
    assert design_check.last_outcome == "retired" and design_check.next_check_at is None

    # Scheduled lifecycle and design sweeps no longer read or count the item.
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *a, **k: pytest.fail("retired item re-read"))
    for options in ({}, {"force": True}):
        result = lifecycle(case, **options)
        assert result.scanned == result.candidate_count == result.never_checked == 0
    sweep = design_sweep(case, activation_timestamp=NOW - timedelta(days=1), discovered_items={})
    assert sweep.scanned == sweep.unavailable == 0

    # Explicit checks still observe Monday but keep the terminal marker.
    def missing(*args, **kwargs):
        raise HTTPException(404, "monday item not found")
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", missing)
    assert lifecycle(case, item_id="123").source_unavailable == 1
    assert design_sweep(case, item_id="123").unavailable == 1
    assert case.db.get(AutoSyncReconciliationCheck, (BOARD, "123", "completed_transition"),
                       populate_existing=True).last_outcome == "retired"
    assert case.db.get(DesignProcessingReconciliationCheck, (BOARD, "123"),
                       populate_existing=True).last_outcome == "retired"


@pytest.mark.parametrize("consumer", ["lifecycle", "design"])
@pytest.mark.parametrize("status", [401, 403, 429, 502])
def test_explicit_api_error_cannot_erase_pre_migration_retirement(case, tracked, monkeypatch, consumer, status):
    from backend.app.models import MondayItemAvailability
    from backend.app.services.monday_item_availability import availability_decision
    retire_deleted_items(case.db, ["123"], fetch_item=purged, dry_run=False)
    # Simulate a retirement performed before migration 0020, as in production.
    case.db.query(MondayItemAvailability).delete()
    case.db.commit()

    def fail(*args, **kwargs):
        raise HTTPException(status, "API read failed")

    if consumer == "lifecycle":
        monkeypatch.setattr(reconciliation, "fetch_item_metadata", fail)
        result = lifecycle(case, item_id="123")
    else:
        gateway = type("Gateway", (), {"fetch_target": staticmethod(fail)})()
        result = reconcile_landing_zone_once(case.db, dry_run=False, access_token="token",
            mode="enabled", now=NOW, gateway=gateway, item_id="123")
    assert result.errors == 1
    assert case.db.get(AutoSyncReconciliationCheck, (BOARD, "123", "completed_transition")).last_outcome == "retired"
    assert case.db.get(DesignProcessingReconciliationCheck, (BOARD, "123")).last_outcome == "retired"
    assert not availability_decision(case.db, BOARD, "123", access_token="token").allowed


def test_item_seen_again_by_a_webhook_returns_to_normal_lifecycle_checks(case, tracked, monkeypatch):
    retire_deleted_items(case.db, ["123"], fetch_item=purged, dry_run=False)
    apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook", desired_source_revision="rev-2")
    # The authenticated webhook boundary observes the current item using its
    # processing credential before dispatching the business-specific policy.
    observe_item(case.db, BOARD, case.item, access_token="token")
    case.db.commit()
    reads = []
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *a, **k: reads.append(a) or dict(case.item))
    result = lifecycle(case)
    assert result.candidate_count == 1 and result.scanned == 0  # Back in scope; normal freshness applies.
    later = datetime.now(timezone.utc) + timedelta(
        seconds=settings.auto_sync_reconciliation_lifecycle_interval_seconds + 1)
    monkeypatch.setattr(reconciliation, "utc_now", lambda: later)
    result = lifecycle(case)
    assert result.scanned == 1 and result.items[0].action == "still_active" and len(reads) == 1
    assert case.db.get(AutoSyncReconciliationCheck, (BOARD, "123", "completed_transition"),
                       populate_existing=True).last_outcome == "still_active"


@pytest.mark.parametrize("state", ["active", "archived", "deleted"])
def test_items_monday_still_returns_are_refused(case, tracked, state):
    [result] = retire_deleted_items(case.db, ["123"], fetch_item=lambda item_id: {"state": state}, dry_run=False)
    assert (result.action, result.monday_state) == ("visible_on_monday", state)
    assert case.db.get(Task, case.task.external_task_key, populate_existing=True).auto_sync_state == "active"
    assert case.db.get(AutoSyncReconciliationCheck, (BOARD, "123", "completed_transition")) is None


def test_dry_run_untracked_and_invalid_ids_change_nothing(case, tracked):
    results = retire_deleted_items(case.db, ["123", "123", "999", "12a"], fetch_item=purged, dry_run=True)
    assert [(r.item_id, r.action) for r in results] == [("123", "would_retire"), ("999", "not_tracked"), ("12a", "error")]
    assert results[0].design_state == "scheduled"
    assert case.db.get(Task, case.task.external_task_key, populate_existing=True).auto_sync_state == "active"
    assert case.db.get(AutoSyncReconciliationCheck, (BOARD, "123", "completed_transition")) is None
    assert case.db.query(DesignProcessingJob).filter_by(item_id="123").one().status == "scheduled"


@pytest.mark.parametrize("dry_run", [True, False])
def test_registered_review_without_task_can_be_retired_while_retaining_pdf_and_history(case, monkeypatch, dry_run):
    monkeypatch.setattr(settings, "performance_specification_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_mode", "enabled")
    monkeypatch.setattr(settings, "design_processing_board_id", BOARD)
    item_id = "456"
    item = DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id=item_id,
        state="ready_for_review", extracted_parameters_json={"retained": "email evidence"}, warnings_json=[])
    case.db.add(item)
    case.db.flush()
    spec = PerformanceSpecification(board_id=BOARD, item_id=item_id, requested_generation=2,
        completed_generation=1, scheduled_for=NOW, lease_token="running", lease_until=NOW + timedelta(hours=1),
        current_revision="retained-revision")
    case.db.add(spec)
    case.db.flush()
    artifact = PerformanceSpecificationArtifact(board_id=BOARD, item_id=item_id, revision="retained-revision",
        translation_json={"values": {"Decking": "Reviewer value"}}, model_version="test", filename="retained.pdf",
        storage_bucket="private", storage_object_key="retained.pdf", content_sha256="digest", size_bytes=100,
        monday_asset_id="attached-pdf", status="published")
    review = PerformanceSpecificationReview(board_id=BOARD, item_id=item_id, monday_account_id="acct",
        source_fingerprint="reviewed-source", source_identity_json={}, decision_digest="decision",
        field_decisions_json={"Decking": {"operation": "specify_value", "display_value": "Reviewer value"}})
    case.db.add_all([artifact, review])
    case.db.flush()
    event_id = uuid.uuid4()
    case.db.add(PerformanceSpecificationReviewEvent(id=event_id, monday_account_id="acct", board_id=BOARD,
        item_id=item_id, actor_app_user_id="reviewer", actor_monday_user_id="100", command_type="save",
        prior_review_version=0, resulting_review_version=0, original_decisions_json={},
        replacement_decisions_json=review.field_decisions_json, base_source_fingerprint="reviewed-source",
        request_digest="decision", result_json={}))
    case.db.commit()
    assert case.db.query(Task).filter_by(board_id=BOARD, item_id=item_id).count() == 0

    [result] = retire_deleted_items(case.db, [item_id], fetch_item=purged, dry_run=dry_run)
    assert (result.action, result.tasks) == ("would_retire" if dry_run else "retired", 0)
    case.db.expire_all()
    assert case.db.get(DesignProcessingItem, item.id).state == ("ready_for_review" if dry_run else "ineligible")
    spec = case.db.get(PerformanceSpecification, (BOARD, item_id))
    assert (spec.scheduled_for is None) is not dry_run
    if not dry_run:
        assert spec.completed_generation == spec.requested_generation and spec.lease_token is None
        check = case.db.get(DesignProcessingReconciliationCheck, (BOARD, item_id))
        assert check.last_outcome == "retired" and check.last_reason == "confirmed_deleted"
    assert spec.current_revision == "retained-revision"
    assert case.db.get(PerformanceSpecificationArtifact, (BOARD, item_id, "retained-revision")).monday_asset_id == "attached-pdf"
    assert case.db.get(PerformanceSpecificationReview, (BOARD, item_id)).field_decisions_json == review.field_decisions_json
    assert case.db.get(PerformanceSpecificationReviewEvent, event_id).request_digest == "decision"
    assert case.db.get(AutoSyncReconciliationCheck, (BOARD, item_id, "completed_transition")) is None
