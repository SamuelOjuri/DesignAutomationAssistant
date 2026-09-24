from copy import deepcopy
from datetime import timedelta
import uuid

import pytest

from backend.app import monday_client
from backend.app.config import settings
from backend.app.models import AutoSyncJob, AutoSyncReconciliationCheck, MondayMetadataLink, TaskFile, TaskSnapshot, TaskMondayMetadata
from backend.app.services import auto_sync, auto_sync_reconciliation as reconciliation, auto_sync_worker, auto_sync_purge
from backend.app.services import monday_metadata as metadata
from backend.app.services.auto_sync_policy import policy_from_settings
from backend.tests.test_monday_metadata import case, refresh


def retained_project(case):
    record = refresh(case)
    revision = auto_sync.compute_desired_source_revision(case.item)
    case.task.latest_snapshot_version = case.task.last_indexed_source_revision = revision
    snapshot = TaskSnapshot(
        id=uuid.uuid4(), external_task_key=case.task.external_task_key,
        snapshot_version=revision, task_context_json=deepcopy(case.item), ingestion_status="complete",
    )
    case.db.add(snapshot)
    case.db.flush()
    case.db.add(TaskFile(
        id=uuid.uuid4(), external_task_key=case.task.external_task_key, snapshot_id=snapshot.id,
        kind="attachment_pdf", monday_asset_id="asset", bucket="private", object_path="retained/drawing.pdf",
    ))
    case.db.commit()
    return record


def lifecycle_check(case, monkeypatch, *, dry_run=False):
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *args: deepcopy(case.item))
    return reconciliation.detect_completed_transitions_once(
        case.db, dry_run=dry_run, access_token="token", policy=policy_from_settings(),
    )


def active_check(case, monkeypatch):
    monkeypatch.setattr(reconciliation, "fetch_current_account_id", lambda token: "acct")
    monkeypatch.setattr(reconciliation, "list_item_ids_in_groups", lambda *args, **kw: {"topics": ["123"]})
    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", lambda *args, **kw: deepcopy(case.item))
    return reconciliation.reconcile_active_items_once(case.db, dry_run=False, access_token="token")


@pytest.mark.parametrize("state", ["archived", "deleted"])
@pytest.mark.parametrize("group", ["topics", "group_mkpbb3tx", "group_mkpbd6vy", None])
def test_source_state_overrides_group_and_never_starts_retention(state, group):
    decision = policy_from_settings().classify_item("1882196103", group, state)
    assert decision.lifecycle_state == state
    assert decision.should_cancel_active_jobs and not decision.should_queue_sync
    assert decision.requires_existing_index


@pytest.mark.parametrize("state", [None, "", "unexpected"])
def test_unknown_source_state_cannot_be_treated_as_active(case, state):
    case.item["state"] = state
    with pytest.raises(ValueError, match="item state"):
        auto_sync.apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook")
    assert case.db.query(AutoSyncJob).count() == case.db.query(TaskMondayMetadata).count() == 0


def test_lifecycle_lookup_requests_actual_state(monkeypatch):
    seen = []
    item = {"id": "3076716400", "state": "archived", "board": {"id": "1882196103"},
            "group": {"id": "group_mkpbs35c", "title": "Hub B - Outstanding"}}
    monkeypatch.setattr(monday_client, "fetch_current_account_id", lambda token: "acct")
    def request(token, query, variables, **kwargs):
        seen.append(query)
        return {"data": {"items": [item]}}
    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    assert monday_client.fetch_item_metadata("token", item["id"])["state"] == "archived"
    assert "state" in seen[0]


@pytest.mark.parametrize("source", ["webhook", "lifecycle", "metadata"])
@pytest.mark.parametrize("job_status", ["scheduled", "running"])
def test_archival_cancels_work_and_preserves_existing_project(case, monkeypatch, source, job_status):
    record = retained_project(case)
    before = (deepcopy(record.fields_json), record.revision, record.checked_at)
    job, _ = auto_sync.coalesce_auto_sync_job(case.db, case.task, trigger_type="webhook")
    job.status = job_status
    job.locked_by = "old-worker" if job_status == "running" else None
    case.task.sync_status = "syncing" if job_status == "running" else "queued"
    case.task.purge_after = metadata.utc_now() - timedelta(days=1)
    metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    case.item["state"] = "archived"
    case.item["group"] = {"id": "group_mkpbs35c", "title": "Hub B - Outstanding"}
    if source == "webhook":
        result = auto_sync.apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook")
        assert result.job is None and not result.metadata_queued
        case.db.commit()
    elif source == "lifecycle":
        result = lifecycle_check(case, monkeypatch)
        assert result.archived == 1 and result.errors == 0
        assert result.items[0].action == "archived"
        assert case.db.query(AutoSyncReconciliationCheck).one().last_reason == "item_archived"
    else:
        assert metadata.run_metadata_once(case.db, "token") == ["ineligible"]
    case.db.refresh(case.task)
    case.db.refresh(job)
    case.db.refresh(record)
    assert case.task.auto_sync_state == "archived" and not case.task.auto_sync_enabled
    assert case.task.sync_status == "completed" and case.task.last_sync_result == "skipped"
    assert case.task.purge_after is None and case.task.completed_at is None
    assert job.status == "cancelled" and job.locked_by is None
    assert record.requested_generation == record.completed_generation
    assert record.scheduled_for is None and record.lease_token is None
    assert (record.fields_json, record.revision, record.checked_at) == before
    assert case.db.query(TaskSnapshot).count() == case.db.query(TaskFile).count() == 1
    assert case.db.query(MondayMetadataLink).count() == 2
    assert metadata.run_metadata_once(case.db, "token") == []
    purge = auto_sync_purge.purge_expired_tasks_once(case.db, dry_run=False, ignore_disabled=True)
    assert purge.scanned == 0


def test_archive_check_includes_tasks_without_completed_ingestion(case, monkeypatch):
    job, _ = auto_sync.coalesce_auto_sync_job(case.db, case.task, trigger_type="webhook")
    case.db.commit()
    case.item["state"] = "archived"
    result = lifecycle_check(case, monkeypatch)
    assert result.archived == 1
    assert job.status == "cancelled" and case.task.auto_sync_state == "archived"


def test_archive_dry_run_preserves_task_queue_and_metadata(case, monkeypatch):
    record = retained_project(case)
    metadata.enqueue_metadata(case.db, case.task)
    case.db.commit()
    generation = record.requested_generation
    case.item["state"] = "archived"
    result = lifecycle_check(case, monkeypatch, dry_run=True)
    assert result.items[0].action == "would_mark_archived"
    assert case.task.auto_sync_state == "active" and case.task.auto_sync_enabled
    assert record.requested_generation == generation > record.completed_generation
    assert case.db.query(AutoSyncReconciliationCheck).count() == 0


@pytest.mark.parametrize("source", ["webhook", "active_reconciliation", "lifecycle"])
def test_restore_reactivates_and_refreshes_without_forcing_documents(case, monkeypatch, source):
    record = retained_project(case)
    original_generation = record.requested_generation
    case.item["state"] = "archived"
    auto_sync.apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook")
    case.db.commit()
    case.item["state"] = "active"
    if source == "webhook":
        auto_sync.apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook", schedule_immediately=True,
            desired_source_revision=auto_sync.compute_desired_source_revision(case.item))
        case.db.commit()
    elif source == "lifecycle":
        result = lifecycle_check(case, monkeypatch)
        assert result.reactivated == 1 and result.items[0].action == "reactivated"
    else:
        assert active_check(case, monkeypatch).errors == 0
    assert case.task.auto_sync_state == "active" and case.task.auto_sync_enabled
    assert record.requested_generation > original_generation
    assert metadata.run_metadata_once(case.db, "token") == ["completed"]
    assert record.requested_generation == record.completed_generation
    assert all(not job.force_requested for job in case.db.query(AutoSyncJob).all())
    assert case.db.query(TaskSnapshot).count() == case.db.query(TaskFile).count() == 1


@pytest.mark.parametrize("group", ["group_mkpbd6vy", "group_mkpbb3tx", "unknown"])
def test_restore_outside_active_groups_does_not_queue(case, monkeypatch, group):
    record = retained_project(case)
    case.item["state"] = "archived"
    auto_sync.apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook")
    case.db.commit()
    generation = record.requested_generation
    case.item["state"] = "active"
    case.item["group"]["id"] = group
    result = lifecycle_check(case, monkeypatch)
    assert result.reactivated == result.errors == 0
    assert case.task.auto_sync_state != "active"
    assert record.requested_generation == generation
    assert case.db.query(AutoSyncJob).count() == 0


def test_active_listing_race_with_archive_reconciles_local_state(case, monkeypatch):
    case.item["state"] = "archived"
    result = active_check(case, monkeypatch)
    assert result.errors == 0 and result.items[0].action == "archived"
    assert case.task.auto_sync_state == "archived"
    assert case.db.query(TaskMondayMetadata).count() == case.db.query(AutoSyncJob).count() == 0


def test_document_worker_cancels_old_job_for_archived_task(case):
    job, _ = auto_sync.coalesce_auto_sync_job(case.db, case.task, trigger_type="webhook", scheduled_for=metadata.utc_now())
    case.task.auto_sync_state = "archived"
    case.task.auto_sync_enabled = False
    case.db.commit()
    def forbidden(*args):
        pytest.fail("Archived task started document processing")
    result = auto_sync_worker.run_due_jobs_once(case.db, access_token="token", pipeline_runner=forbidden)
    assert result.claimed == 0 and job.status == "cancelled"


def test_archival_invalidates_an_inflight_metadata_read(case):
    record = retained_project(case)
    metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    claim = metadata.claim_refresh(case.db, account_id="acct")
    auto_sync.apply_auto_sync_policy_for_item(case.db, {**case.item, "state": "archived"}, trigger_type="webhook")
    case.db.commit()
    assert metadata.execute_refresh(case.db, claim, "token") == "lease_lost"
    assert case.task.auto_sync_state == "archived"
    assert record.requested_generation == record.completed_generation


def test_late_archived_read_cannot_overwrite_restoration(case, monkeypatch):
    retained_project(case)
    metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    claim = metadata.claim_refresh(case.db, account_id="acct")
    def fetch(*args):
        auto_sync.apply_auto_sync_policy_for_item(case.db, {**case.item, "state": "archived"}, trigger_type="webhook")
        auto_sync.apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook")
        case.db.commit()
        return {**case.item, "state": "archived"}
    monkeypatch.setattr(metadata, "fetch_monday_metadata", fetch)
    assert metadata.execute_refresh(case.db, claim, "token") == "lease_lost"
    assert case.task.auto_sync_state == "active" and case.task.auto_sync_enabled


def test_restoration_respects_disabled_auto_sync(case, monkeypatch):
    case.item["state"] = "archived"
    auto_sync.apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook")
    case.db.commit()
    case.item["state"] = "active"
    monkeypatch.setattr(settings, "auto_sync_enabled", False)
    result = lifecycle_check(case, monkeypatch)
    assert result.items[0].action == "reactivation_disabled"
    assert result.reactivated == 0 and case.task.auto_sync_state == "archived"
    assert case.db.query(AutoSyncJob).count() == 0


@pytest.mark.parametrize("stage", ["claimed", "expired"])
def test_document_worker_does_not_run_or_retry_archived_claim(case, stage):
    job, _ = auto_sync.coalesce_auto_sync_job(case.db, case.task, trigger_type="webhook", scheduled_for=metadata.utc_now())
    case.db.commit()
    auto_sync_worker.claim_due_jobs(case.db, worker_id="worker")
    case.task.auto_sync_state = "archived"
    case.task.auto_sync_enabled = False
    job.heartbeat_at = metadata.utc_now() - timedelta(hours=2)
    case.db.commit()
    def forbidden(*args):
        pytest.fail("Archived task ran document processing")
    if stage == "claimed":
        assert auto_sync_worker.execute_claimed_job(case.db, job.id, worker_id="worker", pipeline_runner=forbidden) == "skipped"
    else:
        assert auto_sync_worker.recover_stuck_jobs(case.db) == 1
    assert job.status == "cancelled"
    assert case.db.query(AutoSyncJob).count() == 1


def test_archival_does_not_create_an_untracked_project(case):
    item = {**case.item, "id": "untracked", "state": "archived"}
    result = auto_sync.apply_auto_sync_policy_for_item(case.db, item, trigger_type="webhook")
    assert result.task is None and result.job is None
    assert case.db.query(TaskMondayMetadata).count() == 0
