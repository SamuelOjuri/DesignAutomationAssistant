"""Operator preview/retry preserves workflow ownership and retained history."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend.app import monday_client
from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import (
    AutoSyncJob, AutoSyncReconciliationCheck, DesignProcessingItem, DesignProcessingJob,
    DesignProcessingReconciliationCheck, MondayItemAvailability, PerformanceSpecification,
    PerformanceSpecificationArtifact, PerformanceSpecificationReview, PerformanceSpecificationReviewEvent,
    Task, TaskMondayMetadata, TaskSnapshot,
)
from backend.app.services import monday_item_availability as availability
from backend.app.services import monday_item_availability_retry as operations


NOW = datetime(2026, 11, 10, 16, tzinfo=timezone.utc)
BOARD, ITEM, TOKEN = "1882196103", "123", "offline-processing-token"


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setattr(settings, "monday_ingestion_access_token", TOKEN)
    monkeypatch.setattr(operations, "utc_now", lambda: NOW)
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *a, **kw: pytest.fail("retry must not read Monday"))
    monkeypatch.setattr(monday_client, "fetch_monday_item_statuses", lambda *a, **kw: pytest.fail("retry must not probe Monday"))
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def register_uuid(connection, _):
        connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine)
    future = NOW + timedelta(days=7)
    with Session(engine, expire_on_commit=False) as db:
        task = Task(external_task_key=f"1:{BOARD}:{ITEM}", account_id="1", board_id=BOARD, item_id=ITEM,
            auto_sync_state="active", auto_sync_enabled=True, source_group_id="topics", sync_status="completed",
            latest_snapshot_version="retained", last_indexed_source_revision="retained")
        design = DesignProcessingItem(board_id=BOARD, item_id=ITEM, state="failed",
            extracted_parameters_json={"retained": "extraction"}, match_result_json={"retained": "match"})
        db.add_all([task, design])
        db.flush()
        pending = AutoSyncJob(board_id=BOARD, item_id=ITEM, external_task_key=task.external_task_key,
            trigger_type="reconciliation", status="retry_wait", scheduled_for=future, next_retry_at=future,
            attempt_count=2)
        failed = DesignProcessingJob(board_id=BOARD, item_id=ITEM, trigger_type="reconciliation",
            status="failed", stage="extracting", scheduled_for=future, next_retry_at=future, attempt_count=3,
            last_error="retained-model-failure", result_json={"retained": "diagnostic"})
        scheduled = DesignProcessingJob(board_id=BOARD, item_id=ITEM, trigger_type="reconciliation",
            status="scheduled", scheduled_for=future, next_retry_at=future)
        metadata = TaskMondayMetadata(external_task_key=task.external_task_key,
            requested_generation=5, completed_generation=3, scheduled_for=future, fields_json={"retained": "crm"})
        spec = PerformanceSpecification(board_id=BOARD, item_id=ITEM,
            requested_generation=5, completed_generation=3, scheduled_for=future,
            checked_at=NOW - timedelta(days=1), current_revision="retained-pdf", source_json={"retained": "source"})
        snapshot = TaskSnapshot(id=uuid.uuid4(), external_task_key=task.external_task_key,
            snapshot_version="retained", task_context_json={"retained": "snapshot"})
        design_check = DesignProcessingReconciliationCheck(board_id=BOARD, item_id=ITEM,
            last_attempted_at=NOW, last_checked_at=NOW, last_outcome="unavailable",
            last_reason="item_unavailable_to_worker", next_check_at=future)
        lifecycle = AutoSyncReconciliationCheck(board_id=BOARD, item_id=ITEM, scope="completed_transition",
            last_attempted_at=NOW, last_checked_at=NOW, last_outcome="source_unavailable", last_reason="source_unavailable")
        review = PerformanceSpecificationReview(board_id=BOARD, item_id=ITEM, monday_account_id="1",
            source_fingerprint="retained-source", source_identity_json={"retained": "identity"},
            decision_digest="a" * 64, field_decisions_json={"Decking": {"retained": "reviewer decision"}})
        artifact = PerformanceSpecificationArtifact(board_id=BOARD, item_id=ITEM, revision="retained-pdf",
            translation_json={"retained": "translation"}, source_json={"retained": "provenance"},
            model_version="retained", filename="retained.pdf", storage_bucket="private",
            storage_object_key="retained.pdf", content_sha256="b" * 64, size_bytes=123, status="published")
        history = PerformanceSpecificationReviewEvent(board_id=BOARD, item_id=ITEM, monday_account_id="1",
            actor_app_user_id="reviewer", actor_monday_user_id="10", command_type="save_decisions",
            prior_review_version=0, resulting_review_version=1, original_decisions_json={},
            replacement_decisions_json={"retained": "decision"}, base_source_fingerprint="retained-source",
            request_digest="c" * 64, result_json={"retained": "receipt"})
        db.add_all([pending, failed, scheduled, metadata, spec, snapshot, design_check,
                    lifecycle, review, artifact, history])
        availability.observe_unavailable(db, BOARD, ITEM, TOKEN, NOW)
        db.commit()
        yield SimpleNamespace(db=db, engine=engine, task=task, design=design, pending=pending,
            failed=failed, scheduled=scheduled, metadata=metadata, spec=spec, snapshot=snapshot,
            design_check=design_check, lifecycle=lifecycle, review=review, artifact=artifact, history=history)
    engine.dispose()


def snapshot_rows(db):
    models = (AutoSyncJob, DesignProcessingItem, DesignProcessingJob, DesignProcessingReconciliationCheck,
              MondayItemAvailability, PerformanceSpecification, Task, TaskMondayMetadata,
              TaskSnapshot, PerformanceSpecificationArtifact, PerformanceSpecificationReview,
              PerformanceSpecificationReviewEvent, AutoSyncReconciliationCheck)
    return {model.__tablename__: [deepcopy(dict(row))
        for row in db.execute(model.__table__.select()).mappings()] for model in models}


def test_default_preview_performs_no_writes_or_remote_calls(case):
    before = snapshot_rows(case.db)

    def reject_write(_connection, _cursor, statement, *_args):
        assert statement.lstrip().split(None, 1)[0].upper() not in {"INSERT", "UPDATE", "DELETE"}

    event.listen(case.engine, "before_cursor_execute", reject_write)
    try:
        result = operations.retry_items(case.db, BOARD, [ITEM])
        assert len(result) == 1 and result[0]["action"] == "would_retry"
        assert result[0]["previous_state"] == "unavailable"
    finally:
        event.remove(case.engine, "before_cursor_execute", reject_write)
    assert snapshot_rows(case.db) == before


def test_apply_wakes_pending_work_without_replaying_failed_models_or_changing_history(case):
    before_failed = deepcopy({column.key: getattr(case.failed, column.key) for column in case.failed.__table__.columns})
    retained = {model: deepcopy({column.key: getattr(model, column.key) for column in model.__table__.columns})
                for model in (case.review, case.artifact, case.snapshot, case.history, case.task, case.design)}
    result = operations.retry_items(case.db, BOARD, [ITEM, ITEM], apply=True)
    assert len(result) == 1 and result[0]["action"] == "retry_requested"
    shared = case.db.get(MondayItemAvailability, (BOARD, ITEM, availability.processing_identity(TOKEN)))
    assert shared.next_check_at is None and shared.consecutive_misses == 1
    assert case.pending.scheduled_for == case.pending.next_retry_at == NOW
    assert case.scheduled.scheduled_for == case.scheduled.next_retry_at == NOW
    assert case.pending.status == "retry_wait" and case.pending.attempt_count == 2
    assert case.metadata.scheduled_for == NOW and case.metadata.requested_generation == 5
    assert case.spec.scheduled_for == NOW and case.spec.checked_at is None
    assert case.spec.requested_generation == 5 and case.spec.current_revision == "retained-pdf"
    assert case.design_check.next_check_at == NOW
    assert case.lifecycle.last_checked_at is None and case.lifecycle.last_outcome == "retry_requested"
    assert {column.key: getattr(case.failed, column.key) for column in case.failed.__table__.columns} == before_failed
    for model, before in retained.items():
        assert {column.key: getattr(model, column.key) for column in model.__table__.columns} == before


@pytest.mark.parametrize("retirement", ["shared", "design", "auto_sync"])
def test_retry_refuses_confirmed_retirement_and_preserves_every_schedule(case, retirement):
    if retirement == "shared":
        availability.record_retired(case.db, BOARD, ITEM, TOKEN, observed_at=NOW + timedelta(seconds=1))
    elif retirement == "design":
        case.design_check.last_outcome, case.design_check.last_reason = "retired", "confirmed_deleted"
    else:
        case.lifecycle.last_outcome, case.lifecycle.last_reason = "retired", "confirmed_deleted"
    case.db.commit()
    before = snapshot_rows(case.db)
    for apply in (False, True):
        result = operations.retry_items(case.db, BOARD, [ITEM], apply=apply)
        assert result == [{"item_id": ITEM, "action": "refused", "reason": "confirmed_deleted"}]
        assert snapshot_rows(case.db) == before


def test_retry_does_not_clear_another_processing_identitys_negative_cache(case):
    other_token = "another-processing-identity"
    row = availability.observe_unavailable(case.db, BOARD, ITEM, other_token, NOW)
    case.db.commit()
    case.db.refresh(row)
    before = deepcopy({column.key: getattr(row, column.key) for column in row.__table__.columns})
    operations.retry_items(case.db, BOARD, [ITEM], access_token=TOKEN, apply=True)
    case.db.refresh(row)
    assert {column.key: getattr(row, column.key) for column in row.__table__.columns} == before
    assert not availability.availability_decision(case.db, BOARD, ITEM, other_token, NOW).allowed


@pytest.mark.parametrize("identity", ["0", "-1", "01", " 123", "123x", "1.5", True])
def test_retry_rejects_nonpositive_or_malformed_item_ids_without_changes(case, identity):
    before = snapshot_rows(case.db)
    with pytest.raises(ValueError, match="positive decimal"):
        operations.retry_items(case.db, BOARD, [identity], apply=True)
    assert snapshot_rows(case.db) == before


def test_retry_does_not_take_over_running_metadata_work(case):
    case.metadata.lease_token = "owned-by-running-refresh"
    case.metadata.lease_until = NOW + timedelta(minutes=5)
    due = case.metadata.scheduled_for
    case.db.commit()
    operations.retry_items(case.db, BOARD, [ITEM], apply=True)
    assert case.metadata.lease_token == "owned-by-running-refresh" and case.metadata.scheduled_for == due
