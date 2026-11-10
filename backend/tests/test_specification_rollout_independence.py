"""Specification rollout cannot pause legacy outputs or active-project ingestion."""

from copy import deepcopy

import pytest

from backend.app.config import settings
from backend.app.models import (
    AutoSyncJob, DesignProcessingArtifact, PerformanceSpecification,
)
from backend.app.services.auto_sync import apply_auto_sync_policy_for_item
from backend.app.services.auto_sync_policy import ACTIVE_JOB_STATUSES
from backend.app.services.design_processing_worker import run_worker_once
from backend.tests.test_design_processing_phase6 import (
    PublicationGateway, _publication_snapshot, _seed_publication, db_session,
)
from backend.tests.test_monday_metadata import case as ingestion_case
from backend.tests.test_performance_specification import (
    BOARD, ITEM, case as specification_case, current, run,
)


def _specification_controls(monkeypatch, mode, allowlist=()):
    monkeypatch.setattr(settings, "performance_specification_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_mode", mode)
    monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", list(allowlist))


def _legacy_worker(db, gateway, storage):
    # Use configured design controls, as the running worker does. Specification
    # controls must never substitute for these defaults.
    return run_worker_once(db, worker_id="rollout-independence", access_token="test-token",
        gateway=gateway, analysis_client=object(), artifact_storage=storage,
        claim_limit=1, recover_leases=False, heartbeat_interval_seconds=0)


@pytest.mark.parametrize("specification_mode,allowlist", [
    ("off", []), ("shadow", []), ("allowlist", [ITEM]), ("allowlist", ["999"]),
])
def test_existing_csv_preview_and_match_report_publish_under_independent_specification_controls(
    db_session, monkeypatch, specification_mode, allowlist,
):
    _specification_controls(monkeypatch, specification_mode, allowlist)
    monkeypatch.setattr(settings, "design_processing_mode", "enabled")
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", [])
    identity, item, job, storage, _ = _seed_publication(db_session)
    gateway = PublicationGateway(_publication_snapshot(identity))

    result = _legacy_worker(db_session, gateway, storage)

    artifacts = db_session.query(DesignProcessingArtifact).all()
    assert result.published == 1 and job.status == "completed"
    assert item.state == "ready_for_review"
    assert (item.latest_published_input_revision, item.latest_published_pipeline_version) == (
        identity.input_revision, identity.pipeline_version)
    assert {artifact.artifact_kind for artifact in artifacts} == {
        "ai_data", "ai_data_pdf", "match_report",
    }
    assert all(artifact.status == "published" and artifact.monday_asset_id for artifact in artifacts)
    uploads = [event for event in gateway.events if event[0] == "upload"]
    assert len(uploads) == 3
    assert {event[2] for event in uploads} == {
        artifact.deterministic_filename for artifact in artifacts
    }
    assert [event[1] for event in uploads].count("file_mkza7y37") == 2
    assert [event[1] for event in uploads].count("file_mm59rntf") == 1


@pytest.mark.parametrize("design_mode,design_allowlist", [
    ("off", []), ("shadow", []), ("allowlist", ["999"]),
])
def test_enabling_specifications_cannot_bypass_legacy_design_publication_gates(
    db_session, monkeypatch, design_mode, design_allowlist,
):
    _specification_controls(monkeypatch, "enabled")
    monkeypatch.setattr(settings, "design_processing_mode", design_mode)
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", design_allowlist)
    identity, item, job, storage, _ = _seed_publication(db_session)
    gateway = PublicationGateway(_publication_snapshot(identity))
    if design_mode != "shadow":
        gateway.fetch_target = lambda _: pytest.fail("Paused legacy publication read Monday")

    result = _legacy_worker(db_session, gateway, storage)

    assert result.published == 0
    assert not any(event[0] in {"update", "upload", "delete"} for event in gateway.events)
    assert item.latest_published_input_revision is None
    if design_mode == "shadow":
        # Legacy shadow may recheck source readiness, but cannot publish any of
        # its already-rendered outputs even when specifications are enabled.
        assert result.claimed == result.redundant == 1 and job.status == "completed"
    else:
        assert result.claimed == 0 and gateway.events == [] and job.status == "scheduled"
    assert {artifact.status for artifact in db_session.query(DesignProcessingArtifact)} == {"rendered"}


@pytest.mark.parametrize("specification_mode", ["enabled", "allowlist"])
@pytest.mark.parametrize("design_mode,design_allowlist", [
    ("off", []), ("shadow", []), ("allowlist", ["999"]),
])
def test_specification_publisher_uses_its_own_rollout_after_extraction_is_available(
    specification_case, monkeypatch, specification_mode, design_mode, design_allowlist,
):
    _specification_controls(monkeypatch, specification_mode, [ITEM])
    monkeypatch.setattr(settings, "design_processing_mode", design_mode)
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", design_allowlist)

    assert run(specification_case) == ["published"]

    record = current(specification_case)
    assert record.current_revision and record.completed_generation == record.requested_generation
    assert len(specification_case.gateway.uploads) == len(specification_case.calls) == 1


@pytest.mark.parametrize("specification_mode,specification_allowlist", [
    ("off", []), ("shadow", []), ("allowlist", ["999"]), ("enabled", []),
])
@pytest.mark.parametrize("group,auto_sync_enabled,expected_state,expected_jobs", [
    ("topics", True, "active", 1),
    ("topics", False, "active", 0),
    ("landing", True, "excluded", 0),
    ("completed", True, "completed_retained", 0),
])
def test_active_ingestion_remains_controlled_by_its_own_switch_and_groups(
    ingestion_case, monkeypatch, specification_mode, specification_allowlist,
    group, auto_sync_enabled, expected_state, expected_jobs,
):
    _specification_controls(monkeypatch, specification_mode, specification_allowlist)
    monkeypatch.setattr(settings, "design_processing_board_id", BOARD)
    monkeypatch.setattr(settings, "design_processing_landing_group_id", "landing")
    monkeypatch.setattr(settings, "auto_sync_enabled", auto_sync_enabled)
    monkeypatch.setattr(settings, "auto_sync_excluded_group_ids", "landing")
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")
    item = deepcopy(ingestion_case.item)
    item["group"]["id"] = group

    result = apply_auto_sync_policy_for_item(ingestion_case.db, item,
        trigger_type="webhook", desired_source_revision="current-email-revision")
    ingestion_case.db.commit()

    assert result.task.auto_sync_state == expected_state
    jobs = ingestion_case.db.query(AutoSyncJob).filter(
        AutoSyncJob.status.in_(ACTIVE_JOB_STATUSES)).all()
    assert len(jobs) == expected_jobs
    assert bool(result.decision.should_queue_sync) is bool(expected_jobs)
    # Auto-sync only cancels out-of-scope specification work; it cannot start a
    # new specification or turn a Landing Zone review into an active chat task.
    assert ingestion_case.db.query(PerformanceSpecification).count() == 0
