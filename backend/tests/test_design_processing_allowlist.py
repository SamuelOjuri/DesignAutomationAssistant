"""Single-item rollout must restrict expensive work, not only Monday writes."""

from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import DesignProcessingItem, DesignProcessingJob
from backend.app.services.auto_sync import utc_now
from backend.app.services.design_processing_inputs import DesignProcessingTargetSnapshot
from backend.app.services.design_processing_queue import execution_allowed_for_item, queue_design_processing_snapshot
from backend.app.services import design_processing_worker as worker
from backend.app.services.design_processing_reconciliation import reconcile_landing_zone_once
from backend.app.monday_client import MondayGroupItem


BOARD = "1882196103"
TARGET = "3320074395"
OTHER = "100"


@pytest.fixture()
def case(monkeypatch):
    monkeypatch.setattr(settings, "design_processing_mode", "allowlist")
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", [TARGET])
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield SimpleNamespace(db=db, now=utc_now())
    engine.dispose()


def add_job(case, item_id, *, age_minutes=1, assigned=False):
    due = case.now - timedelta(minutes=age_minutes)
    version = settings.design_processing_pipeline_version
    item = DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id=item_id,
        state="scheduled", warnings_json=[], latest_desired_input_revision="email-v1",
        latest_desired_pipeline_version=version, created_at=due, updated_at=due)
    job = DesignProcessingJob(id=uuid.uuid4(), board_id=BOARD, item_id=item_id,
        trigger_type="test", status="scheduled", scheduled_for=due, attempt_count=1 if assigned else 0,
        created_at=due, updated_at=due, execution_kind="analysis" if assigned else None,
        execution_input_revision="email-v1" if assigned else None,
        execution_pipeline_version=version if assigned else None, stage="extracting" if assigned else None)
    case.db.add_all([item, job])
    case.db.commit()
    return item, job


@pytest.mark.parametrize("kind", ["analysis", "publication"])
@pytest.mark.parametrize("mode,listed,allowed", [("off", True, False), ("shadow", False, None),
                                              ("enabled", False, True), ("allowlist", False, False),
                                              ("allowlist", True, True)])
def test_policy_gates_analysis_and_publication(kind, mode, listed, allowed):
    if allowed is None:
        allowed = kind == "analysis"
    assert execution_allowed_for_item(mode=mode, execution_kind=kind, item_id=TARGET,
                                     allowlist_item_ids=[TARGET] if listed else []) is allowed


@pytest.mark.parametrize("claim", [worker.claim_due_analysis_jobs, worker.claim_due_design_processing_jobs])
def test_claim_filters_before_limit_and_preserves_older_unlisted_job(case, claim):
    _, other = add_job(case, OTHER, age_minutes=60)
    _, target = add_job(case, TARGET)
    claimed = claim(case.db, worker_id="test-worker", mode="allowlist", allowlist_item_ids=[TARGET], limit=1)
    assert [job.id for job in claimed] == [target.id]
    case.db.refresh(other)
    assert other.status == "scheduled" and other.attempt_count == 0 and other.locked_by is None


def test_nonallowlisted_backlog_causes_no_source_model_or_storage_calls(case):
    add_job(case, OTHER, age_minutes=60)
    def unexpected(*args, **kwargs):
        pytest.fail("Unlisted item triggered external work")
    dependency = SimpleNamespace(fetch_target=unexpected, extract_design_parameters=unexpected,
                                 read_private=unexpected, write_private=unexpected)
    result = worker.run_worker_once(case.db, access_token="unused", gateway=dependency,
                                   analysis_client=dependency, artifact_storage=dependency)
    assert result.claimed == result.analyzed == result.published == 0
    assert case.db.query(DesignProcessingJob).one().status == "scheduled"


def snapshot(item_id):
    return DesignProcessingTargetSnapshot(board_id=BOARD, item_id=item_id, group_id="topics",
        item_state="active", name="Roof enquiry", email_assets=(object(),), input_revision="email-v1")


def test_unlisted_admission_preserves_existing_work_and_does_not_create_jobs(case):
    item, job = add_job(case, OTHER)
    original_due = job.scheduled_for
    result = queue_design_processing_snapshot(case.db, snapshot(OTHER), trigger_type="reconciliation",
        mode="allowlist", allowlist_item_ids=[TARGET], pipeline_version=settings.design_processing_pipeline_version,
        expected_board_id=BOARD, expected_group_id=settings.design_processing_landing_group_id, now=case.now)
    case.db.commit()
    assert result.outcome == "excluded" and result.readiness == "not_allowlisted"
    assert job.status == "scheduled" and job.scheduled_for == original_due
    assert item.latest_desired_pipeline_version == settings.design_processing_pipeline_version
    assert case.db.query(DesignProcessingJob).count() == 1


def test_repeated_reconciliation_keeps_original_queue_priority(case):
    _, job = add_job(case, TARGET, age_minutes=60)
    original_due = job.scheduled_for
    for offset in (0, 5, 10):
        result = queue_design_processing_snapshot(case.db, snapshot(TARGET), trigger_type="reconciliation",
            mode="allowlist", allowlist_item_ids=[TARGET], pipeline_version=settings.design_processing_pipeline_version,
            expected_board_id=BOARD, expected_group_id=settings.design_processing_landing_group_id,
            now=case.now + timedelta(minutes=offset))
        case.db.commit()
        assert result.outcome == "coalesced" and job.scheduled_for == original_due
    assert case.db.query(DesignProcessingJob).count() == 1


def test_broad_reconciliation_filters_before_limit_and_only_reads_test_item(case):
    calls = []
    def fetch(item_id):
        calls.append(item_id)
        assert item_id == TARGET
        return snapshot(item_id)
    result = reconcile_landing_zone_once(case.db, dry_run=False, mode="allowlist", access_token="unused",
        gateway=SimpleNamespace(fetch_target=fetch), activation_timestamp=case.now - timedelta(days=1),
        discovered_items={"topics": [MondayGroupItem(OTHER, case.now.isoformat()), MondayGroupItem(TARGET, case.now.isoformat())]},
        limit=1, now=case.now)
    assert calls == [TARGET] and result.queued == 1 and result.errors == 0
    assert [job.item_id for job in case.db.query(DesignProcessingJob).all()] == [TARGET]


def test_already_claimed_unlisted_job_defers_before_reading_sources(case):
    _, job = add_job(case, OTHER)
    worker.claim_due_design_processing_jobs(case.db, worker_id="test-worker", mode="enabled")
    def unexpected(*args, **kwargs):
        pytest.fail("Deferred job made an external call")
    outcome = worker.execute_claimed_analysis_job(case.db, job.id, worker_id="test-worker",
        access_token="unused", gateway=SimpleNamespace(fetch_target=unexpected),
        analysis_client=SimpleNamespace(extract_design_parameters=unexpected), artifact_storage=None,
        mode="allowlist", heartbeat_interval_seconds=0)
    assert outcome == "deferred"
    case.db.refresh(job)
    assert job.status == "scheduled" and job.locked_by is None and job.attempt_count == 0


def test_expired_unlisted_execution_is_held_and_can_be_claimed_when_enabled(case):
    item, job = add_job(case, OTHER, age_minutes=120, assigned=True)
    job.status, job.locked_by = "running", "old-worker"
    job.locked_at = job.heartbeat_at = case.now - timedelta(hours=2)
    case.db.commit()
    recovered = worker.recover_expired_design_processing_leases(case.db, mode="allowlist", allowlist_item_ids=[TARGET], now=case.now)
    assert recovered == 1
    case.db.refresh(job)
    assert job.status == "scheduled" and job.stage == "extracting" and job.attempt_count == 1
    assert job.execution_input_revision == "email-v1" and job.locked_by is None
    assert not worker.claim_due_design_processing_jobs(case.db, worker_id="test-worker", mode="allowlist", allowlist_item_ids=[TARGET])
    assert [claimed.id for claimed in worker.claim_due_design_processing_jobs(case.db, worker_id="test-worker", mode="enabled")] == [job.id]
