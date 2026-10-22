from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import AutoSyncJob, AutoSyncReconciliationCheck, MondayMetadataLink, Task, TaskMondayMetadata, TaskSnapshot
from backend.app.services import auto_sync_reconciliation as reconciliation, monday_metadata as metadata
from backend.app.services.auto_sync import apply_auto_sync_policy_for_item, compute_desired_source_revision
from backend.app.services.auto_sync_policy import policy_from_settings
from backend.app.services.monday_metadata_fields import normalize_fields
from backend.tests.test_monday_metadata import case, refresh


@pytest.fixture()
def source(case, monkeypatch):
    monkeypatch.setattr(settings, "auto_sync_reconciliation_active_interval_seconds", 3600)
    monkeypatch.setattr(settings, "auto_sync_reconciliation_lifecycle_interval_seconds", 3600)
    now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(reconciliation, "utc_now", lambda: now)
    revision = compute_desired_source_revision(case.item)
    case.task.last_indexed_source_revision = case.task.latest_snapshot_version = revision
    case.db.add(TaskSnapshot(
        id=uuid.uuid4(), external_task_key=case.task.external_task_key,
        snapshot_version=revision, task_context_json=deepcopy(case.item), ingestion_status="complete",
    ))
    case.db.commit()
    calls = []
    monkeypatch.setattr(reconciliation, "fetch_current_account_id", lambda token: "acct")
    monkeypatch.setattr(reconciliation, "list_item_ids_in_groups", lambda *args, **kw: {
        case.item["group"]["id"]: ["123"],
    })

    def fetch(*args, **kwargs):
        calls.append("source")
        return deepcopy(case.item)

    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", fetch)
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *args, **kw: pytest.fail("duplicate lifecycle read"))
    return now, calls


def active(case, **kwargs):
    return reconciliation.reconcile_active_items_once(case.db, access_token="token", dry_run=False, **kwargs)


def lifecycle(case, **kwargs):
    return reconciliation.detect_completed_transitions_once(case.db, access_token="token", dry_run=False, **kwargs)


def test_one_source_read_serves_crm_and_lifecycle_and_survives_restart(case, source):
    _, calls = source
    assert active(case).items[0].action == "fresh"
    record = case.db.get(TaskMondayMetadata, case.task.external_task_key)
    assert record.fields_json == normalize_fields(case.item)
    assert record.requested_generation == record.completed_generation
    assert metadata.run_metadata_once(case.db, "token") == []
    with Session(case.db.bind) as restarted:
        result = reconciliation.reconcile_active_items_once(restarted, access_token="token", dry_run=False)
        assert result.scanned == 0 and result.candidate_count == 1 and result.never_checked == 0
        result = reconciliation.detect_completed_transitions_once(restarted, access_token="token", dry_run=False)
        assert result.scanned == 0 and result.candidate_count == 1 and result.never_checked == 0
    assert calls == ["source"] and case.calls == []


def test_expired_source_check_detects_change_without_duplicate_metadata_read(case, source, monkeypatch):
    now, calls = source
    active(case)
    monkeypatch.setattr(reconciliation, "utc_now", lambda: now + timedelta(seconds=3600))
    case.item["assets"] = [{"id": "new", "name": "drawing.pdf"}]
    case.item["column_values"][4]["values"] = [{"id": "87", "label": "RG"}]
    result = active(case)
    assert result.queued == 1 and result.items[0].refresh_reason == "stale"
    assert case.db.query(AutoSyncJob).one().desired_source_revision == compute_desired_source_revision(case.item)
    assert case.db.get(TaskMondayMetadata, case.task.external_task_key).fields_json == normalize_fields(case.item)
    assert metadata.run_metadata_once(case.db, "token") == []
    assert calls == ["source", "source"]


@pytest.mark.parametrize("change", ["failed", "stuck", "missing_snapshot", "group_move", "inactive"])
def test_local_repairs_and_observed_group_moves_bypass_active_freshness(case, source, change):
    active(case)
    if change == "failed":
        case.task.sync_status = "failed"
    elif change == "stuck":
        case.task.sync_status = "syncing"
    elif change == "missing_snapshot":
        case.db.query(TaskSnapshot).delete()
    elif change == "group_move":
        case.item["group"]["id"] = "group_mkpbs35c"
    else:
        case.task.auto_sync_state = "archived"
        case.task.auto_sync_enabled = False
    case.db.commit()
    result = active(case)
    assert result.scanned == 1 and result.errors == 0
    assert result.queued == int(change in {"failed", "stuck", "missing_snapshot"})
    assert case.task.source_group_id == case.item["group"]["id"]
    assert case.task.auto_sync_state == "active" and case.task.auto_sync_enabled


def test_crm_only_change_updates_values_without_document_work(case, source, monkeypatch):
    now, _ = source
    active(case)
    case.item["column_values"][4]["values"] = [{"id": "87", "label": "RG"}]
    monkeypatch.setattr(reconciliation, "utc_now", lambda: now + timedelta(hours=1))
    result = active(case)
    assert result.scanned == 1 and result.queued == 0 and result.items[0].action == "fresh"
    assert case.db.query(AutoSyncJob).count() == 0
    assert case.db.get(TaskMondayMetadata, case.task.external_task_key).fields_json == normalize_fields(case.item)
    assert metadata.run_metadata_once(case.db, "token") == []


@pytest.mark.parametrize("pending", ["before_read", "during_read", "leased"])
def test_reused_read_cannot_consume_or_overwrite_webhook_refresh(case, source, monkeypatch, pending):
    record = refresh(case)
    original = deepcopy(record.fields_json)
    if pending != "during_read":
        metadata.enqueue_metadata(case.db, case.task, immediate=True)
        case.db.commit()
    claim = metadata.claim_refresh(case.db, account_id="acct") if pending == "leased" else None

    def fetch(*args, **kwargs):
        old = deepcopy(case.item)
        if pending == "during_read":
            metadata.enqueue_metadata(case.db, case.task, immediate=True)
            case.db.commit()
        case.item["column_values"][4]["values"] = [{"id": "87", "label": "RG"}]
        return old

    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", fetch)
    assert active(case).errors == 0
    case.db.refresh(record)
    assert record.fields_json == original
    assert record.requested_generation > record.completed_generation
    if claim:
        assert metadata.execute_refresh(case.db, claim, "token") == "completed"
    else:
        assert metadata.run_metadata_once(case.db, "token") == ["completed"]
    assert record.fields_json == normalize_fields(case.item)


def test_incomplete_crm_read_retains_values_and_dependencies_and_queues_retry(case, source):
    record = refresh(case)
    before = deepcopy(record.fields_json), record.revision, record.checked_at
    case.item["column_values"][0]["linked_items"] = []
    assert active(case).errors == 0
    case.db.refresh(record)
    assert (record.fields_json, record.revision, record.checked_at) == before
    assert record.requested_generation > record.completed_generation
    assert case.db.query(MondayMetadataLink).count() == 2
    assert metadata.run_metadata_once(case.db, "token") == ["failed"]
    assert record.fields_json == before[0]


def test_force_rechecks_success_and_errors_remain_due(case, source, monkeypatch):
    active(case)
    assert active(case).scanned == 0

    def fail(*args, **kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", fail)
    assert active(case, force=True).errors == 1
    # The previous successful timestamp must not mask the failed forced attempt.
    assert active(case).errors == 1


def test_new_item_reuses_initial_source_read_for_crm(case, source, monkeypatch):
    item = {**deepcopy(case.item), "id": "124"}
    monkeypatch.setattr(reconciliation, "list_item_ids_in_groups", lambda *a, **kw: {"topics": ["124"]})
    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", lambda *a, **kw: item)
    result = active(case)
    assert result.queued == 1 and result.items[0].refresh_reason == "missing"
    record = case.db.get(TaskMondayMetadata, "acct:1882196103:124")
    assert record.fields_json == normalize_fields(item)
    assert record.requested_generation == record.completed_generation
    assert metadata.run_metadata_once(case.db, "token") == []


@pytest.mark.parametrize("event", ["direct", "linked"])
def test_webhook_refresh_still_runs_while_source_check_is_fresh(case, source, event):
    active(case)
    case.item["column_values"][3]["mirrored_items"][0]["mirrored_value"]["text"] = "Updated project"
    if event == "direct":
        result = apply_auto_sync_policy_for_item(
            case.db, case.item, trigger_type="webhook", schedule_immediately=True,
            desired_source_revision=compute_desired_source_revision(case.item),
        )
        assert result.metadata_queued and result.job is None
    else:
        assert metadata.enqueue_linked_dependents(case.db, "1825117125", "601") == 1
        case.db.flush()
        case.db.get(TaskMondayMetadata, case.task.external_task_key).scheduled_for = metadata.utc_now()
    case.db.commit()
    assert active(case).scanned == 0
    assert metadata.run_metadata_once(case.db, "token") == ["completed"]
    assert case.db.get(TaskMondayMetadata, case.task.external_task_key).fields_json == normalize_fields(case.item)


def test_zero_intervals_disable_deferral(case, source, monkeypatch):
    active(case)
    monkeypatch.setattr(settings, "auto_sync_reconciliation_active_interval_seconds", 0)
    monkeypatch.setattr(settings, "auto_sync_reconciliation_lifecycle_interval_seconds", 0)
    assert active(case).scanned == 1
    monkeypatch.setattr(reconciliation, "fetch_item_metadata", lambda *a, **kw: deepcopy(case.item))
    assert lifecycle(case).scanned == 1


@pytest.mark.parametrize("wrong", ["id", "board"])
def test_wrong_identity_cannot_refresh_metadata_or_claim_lifecycle_coverage(case, source, monkeypatch, wrong):
    item = deepcopy(case.item)
    item[wrong] = "456" if wrong == "id" else {"id": "other-board"}
    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", lambda *a, **kw: item)
    assert active(case).errors == 1
    assert case.db.query(TaskMondayMetadata).count() == 0
    assert case.db.query(AutoSyncReconciliationCheck).filter_by(scope="completed_transition").count() == 0


def test_lifecycle_freshness_expires_and_targeted_and_forced_checks_bypass_it(case, source, monkeypatch):
    now, _ = source
    active(case)
    assert lifecycle(case).scanned == 0
    calls = []

    def fetch(*args, **kwargs):
        calls.append("lifecycle")
        return deepcopy(case.item)

    monkeypatch.setattr(reconciliation, "fetch_item_metadata", fetch)
    assert lifecycle(case, item_id="123").scanned == 1
    assert lifecycle(case, external_task_key=case.task.external_task_key).scanned == 1
    assert lifecycle(case, force=True).scanned == 1
    assert lifecycle(case).scanned == 0
    monkeypatch.setattr(reconciliation, "utc_now", lambda: now + timedelta(hours=1))
    case.item["state"] = "archived"
    assert lifecycle(case).archived == 1
    assert case.task.auto_sync_state == "archived"
    assert len(calls) == 4


@pytest.mark.parametrize("failure", [RuntimeError("failure"), HTTPException(404, "monday item not found")])
def test_failed_lifecycle_checks_do_not_become_fresh(case, source, monkeypatch, failure):
    active(case)

    def fetch(*args, **kwargs):
        raise failure

    monkeypatch.setattr(reconciliation, "fetch_item_metadata", fetch)
    lifecycle(case, force=True)
    result = lifecycle(case)
    assert result.scanned == 1
    assert result.errors + result.source_unavailable == 1


def test_dry_run_writes_no_metadata_or_progress(case, source):
    result = reconciliation.reconcile_active_items_once(case.db, access_token="token", dry_run=True)
    assert result.scanned == 1 and result.queued == 0
    assert case.db.query(TaskMondayMetadata).count() == 0
    assert case.db.query(AutoSyncReconciliationCheck).count() == 0
    assert case.db.query(MondayMetadataLink).count() == 0


def test_sweeps_have_finite_defaults_and_rotate_past_the_first_batch(case, source, monkeypatch):
    monkeypatch.setattr(settings, "auto_sync_reconciliation_completed_batch_size", 2)
    for item_id in ("124", "125", "126", "127"):
        case.db.add(Task(
            external_task_key=f"acct:1882196103:{item_id}", account_id="acct", board_id="1882196103",
            item_id=item_id, auto_sync_enabled=True, auto_sync_state="active", sync_status="completed",
        ))
    case.db.commit()
    calls = []

    def fetch(token, item_id, **kwargs):
        calls.append(item_id)
        return {**deepcopy(case.item), "id": item_id}

    monkeypatch.setattr(reconciliation, "fetch_item_metadata", fetch)
    assert lifecycle(case).scanned == 2
    assert lifecycle(case).scanned == 2
    assert lifecycle(case).scanned == 1
    assert sorted(calls) == ["123", "124", "125", "126", "127"]
    monkeypatch.setattr(reconciliation, "list_item_ids_in_groups", lambda *a, **kw: {"topics": calls})
    monkeypatch.setattr(reconciliation, "fetch_current_source_revision_inputs", fetch)
    policy = replace(policy_from_settings(), backfill_batch_size=2)
    assert active(case, policy=policy).scanned == 2
    assert active(case, policy=policy, force=True).scanned == 2


@pytest.mark.parametrize("phase", ["active", "lifecycle"])
@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_limits_fail_before_remote_reads(case, source, phase, limit):
    with pytest.raises(ValueError):
        (active if phase == "active" else lifecycle)(case, limit=limit)
    assert source[1] == []


def test_cli_passes_force_without_removing_batch_limits(monkeypatch):
    seen = []
    monkeypatch.setattr("sys.argv", ["reconcile", "--force", "--limit", "3", "--completed-transitions", "--completed-limit", "2"])
    monkeypatch.setattr(reconciliation, "SessionLocal", lambda: type("DB", (), {"close": lambda self: None})())

    def run(db, **kwargs):
        seen.append(kwargs)
        return reconciliation.ReconciliationResult(dry_run=False, board_id="1882196103")

    monkeypatch.setattr(reconciliation, "reconcile_active_items_once", run)
    monkeypatch.setattr(reconciliation, "detect_completed_transitions_once", run)
    assert reconciliation.main() == 0
    assert [kwargs["force"] for kwargs in seen] == [True, True]
    assert [kwargs["limit"] for kwargs in seen] == [3, 2]
