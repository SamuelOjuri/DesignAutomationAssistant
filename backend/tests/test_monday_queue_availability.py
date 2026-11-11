"""Queued metadata/ingestion must obey shared holds before any Monday API work."""

from datetime import timedelta

import pytest

from backend.app.models import MondayItemAvailability, TaskMondayMetadata
from backend.app.monday_client import MondayItemUnavailable
from backend.app.services import auto_sync_worker, monday_metadata
from backend.app.services.auto_sync import coalesce_auto_sync_job
from backend.app.services.monday_item_availability import observe_unavailable, record_retired
from backend.tests.test_monday_metadata import case


def metadata_queue(case):
    monday_metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    return case.db.get(TaskMondayMetadata, case.task.external_task_key)


@pytest.mark.parametrize("state", ["retired", "unavailable"])
def test_suppressed_metadata_claim_does_not_read_identity_or_item(case, monkeypatch, state):
    metadata_queue(case)
    observer = record_retired if state == "retired" else observe_unavailable
    observer(case.db, case.task.board_id, case.task.item_id, access_token="token")
    case.db.commit()
    monkeypatch.setattr(monday_metadata, "fetch_current_account_id", lambda _: pytest.fail("Suppressed metadata read account"))
    monkeypatch.setattr(monday_metadata, "fetch_monday_metadata", lambda *args: pytest.fail("Suppressed metadata read item"))
    assert monday_metadata.run_metadata_once(case.db, "token") == []


def test_metadata_missing_lookup_keeps_values_and_uses_shared_deadline_without_attempt(case, monkeypatch):
    record = metadata_queue(case)
    record.revision = "retained-revision"
    record.fields_json = [{"retained": "existing CRM recommendations"}]
    case.db.commit()
    calls = []
    def missing(*args):
        calls.append(1)
        raise MondayItemUnavailable()
    monkeypatch.setattr(monday_metadata, "fetch_monday_metadata", missing)
    assert monday_metadata.run_metadata_once(case.db, "token") == ["failed"]
    observed = case.db.query(MondayItemAvailability).one()
    case.db.refresh(record)
    assert record.attempt_count == 0 and record.revision == "retained-revision"
    assert record.fields_json == [{"retained": "existing CRM recommendations"}]
    assert record.scheduled_for == observed.next_check_at
    assert monday_metadata.run_metadata_once(case.db, "token") == [] and calls == [1]


@pytest.mark.parametrize("state", ["retired", "unavailable"])
def test_suppressed_ingestion_claim_does_not_start_pipeline(case, state):
    now = auto_sync_worker.utc_now()
    coalesce_auto_sync_job(case.db, case.task, trigger_type="webhook",
        desired_source_revision="new-email", scheduled_for=now)
    observer = record_retired if state == "retired" else observe_unavailable
    observer(case.db, case.task.board_id, case.task.item_id, access_token="token", observed_at=now)
    case.db.commit()
    result = auto_sync_worker.run_due_jobs_once(case.db, worker_id="test", access_token="token",
        pipeline_runner=lambda *args: pytest.fail("Suppressed ingestion started pipeline"))
    assert result.claimed == 0


def test_ingestion_missing_lookup_uses_shared_deadline_without_attempt_burn(case):
    now = auto_sync_worker.utc_now()
    job, _ = coalesce_auto_sync_job(case.db, case.task, trigger_type="webhook",
        desired_source_revision="new-email", scheduled_for=now)
    case.db.commit()
    [claim] = auto_sync_worker.claim_due_jobs(case.db, worker_id="test", access_token="token",
        now=now + timedelta(seconds=1))
    calls = []
    def missing(*args):
        calls.append(1)
        raise MondayItemUnavailable()
    assert auto_sync_worker.execute_claimed_job(case.db, claim.id, worker_id="test",
        access_token="token", pipeline_runner=missing, heartbeat_interval_seconds=0) == "retry_wait"
    case.db.refresh(job)
    observed = case.db.query(MondayItemAvailability).one()
    assert job.attempt_count == 0 and job.status == "retry_wait"
    assert job.next_retry_at == observed.next_check_at
    assert case.task.sync_status == "queued"
    assert auto_sync_worker.claim_due_jobs(case.db, worker_id="test", access_token="token") == []
    assert calls == [1]
