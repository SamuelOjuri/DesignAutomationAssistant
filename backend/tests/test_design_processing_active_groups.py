from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import multiprocessing
import time
from types import SimpleNamespace

import pytest

from backend.app.config import settings
from backend.app.models import DesignProcessingArtifact, DesignProcessingJob
from backend.app.services.auto_sync import utc_now
from backend.app.services import design_processing_reconciliation as reconciliation
from backend.app.services.design_processing_execution import _run_in_process, run_interruptible_analysis
from backend.app.services.design_processing_inputs import (
    DesignEmailAsset, DownloadedDesignEmailAsset, parse_design_processing_target,
)
from backend.app.services.design_processing_operations import retry_failed_design_processing_job
from backend.app.services.design_processing_pipeline import cleanup_delete_pending_artifacts
from backend.app.services.design_processing_queue import queue_design_processing_snapshot
from backend.app.services.design_processing_state import ProcessingIdentity
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.tests import test_design_processing_phase6 as publication


db_session = publication.db_session
DATE = "date_mkpb23av"
HOUR = "hour_mkpbb3j1"
ZIP = "dropdown_mkpbafca"
ACTIVE = "topics"


@pytest.fixture(autouse=True)
def group_config(monkeypatch):
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics,active_b")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")


def _queue(db, snapshot):
    result = queue_design_processing_snapshot(
        db, snapshot, trigger_type="test_move", mode="enabled",
        pipeline_version=settings.design_processing_pipeline_version,
        expected_board_id="1882196103", expected_group_id="group_mkpbd6vy", now=utc_now(),
    )
    db.commit()
    return result


@pytest.mark.parametrize("existing, expected", [
    ({DATE: '{"date":"2020-01-01"}', HOUR: '{"hour":0,"minute":0}', ZIP: '{"ids":[9]}'}, set()),
    ({DATE: '{"date":"2020-01-01"}', HOUR: None, ZIP: '{"ids":[]}'}, {HOUR, ZIP}),
    ({DATE: None, HOUR: "null", ZIP: "{}"}, {DATE, HOUR, ZIP}),
    ({DATE: "unreadable", HOUR: {"unexpected": 1}}, set()),
    ({DATE: '{"date":null,"time":null}', HOUR: '{"hour":null,"minute":null}', ZIP: None}, {DATE, HOUR, ZIP}),
])
def test_active_publication_preserves_values_and_still_uploads_all_files(db_session, existing, expected):
    identity, item, job, storage, _ = publication._seed_publication(db_session)
    snapshot = replace(publication._publication_snapshot(identity), group_id=ACTIVE, scalar_column_values=existing)
    gateway = publication.PublicationGateway(snapshot)
    result = publication._run_publication(db_session, gateway, storage)
    assert result.published == 1
    writes = [event[1] for event in gateway.events if event[0] == "update"]
    assert (set(writes[0]) if writes else set()) == expected
    assert len([event for event in gateway.events if event[0] == "upload"]) == 3
    assert item.state == "ready_for_review"
    assert job.execution_kind == "publication"


def test_move_during_publication_uses_latest_group_and_values(db_session):
    identity, _, _, storage, _ = publication._seed_publication(db_session)

    class MovingGateway(publication.PublicationGateway):
        def fetch_design_owned_column_settings(self, board_id):
            result = super().fetch_design_owned_column_settings(board_id)
            self.snapshot = replace(self.snapshot, group_id=ACTIVE, scalar_column_values={
                DATE: '{"date":"2024-01-01"}', HOUR: None, ZIP: None,
            })
            return result

    gateway = MovingGateway(publication._publication_snapshot(identity))
    assert publication._run_publication(db_session, gateway, storage).published == 1
    write = next(event[1] for event in gateway.events if event[0] == "update")
    assert set(write) == {HOUR, ZIP}


def test_retry_rereads_values_written_since_failed_update(db_session):
    identity, _, job, storage, _ = publication._seed_publication(db_session)

    class FailingGateway(publication.PublicationGateway):
        fail_once = True

        def update_design_owned_columns(self, *args):
            super().update_design_owned_columns(*args)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("uncertain scalar update")

    snapshot = replace(publication._publication_snapshot(identity), group_id=ACTIVE,
                       scalar_column_values={DATE: None, HOUR: None, ZIP: None})
    gateway = FailingGateway(snapshot)
    assert publication._run_publication(db_session, gateway, storage).retry_wait == 1
    gateway.snapshot = replace(snapshot, scalar_column_values={
        DATE: '{"date":"2025-02-03"}', HOUR: '{"hour":12,"minute":30}', ZIP: '{"ids":[9]}',
    })
    job.scheduled_for = utc_now() - timedelta(seconds=1)
    job.next_retry_at = None
    db_session.commit()
    assert publication._run_publication(db_session, gateway, storage).published == 1
    assert len([event for event in gateway.events if event[0] == "update"]) == 1


@pytest.mark.parametrize("completed_gate, uploads", [(1, 0), (2, 0), (3, 0), (4, 1), (5, 2), (6, 3)])
def test_completed_folder_stops_each_publication_boundary(db_session, completed_gate, uploads):
    identity, item, job, storage, _ = publication._seed_publication(db_session)

    class CompletingGateway(publication.PublicationGateway):
        def fetch_target(self, item_id):
            count = sum(event[0] == "gate" for event in self.events) + 1
            if count >= completed_gate:
                self.snapshot = replace(self.snapshot, group_id="completed")
            return super().fetch_target(item_id)

    gateway = CompletingGateway(publication._publication_snapshot(identity))
    result = publication._run_publication(db_session, gateway, storage)
    assert result.cancelled == 1
    assert item.state == "ineligible"
    assert job.last_error == "completed_folder"
    assert job.status == "cancelled"
    assert item.latest_published_input_revision is None
    assert sum(event[0] == "upload" for event in gateway.events) == uploads
    assert _queue(db_session, gateway.snapshot).outcome == "excluded"
    assert db_session.query(DesignProcessingJob).count() == 1


def test_active_moves_preserve_running_execution_and_completed_wins(db_session, monkeypatch):
    identity, item, job, _, _ = publication._seed_publication(db_session)
    job.status = "running"
    job.execution_kind = "analysis"
    job.execution_input_revision = identity.input_revision
    job.execution_pipeline_version = identity.pipeline_version
    job.stage = "extracting"
    job.locked_by = "worker"
    job.next_retry_at = utc_now() + timedelta(minutes=10)
    item.state = "processing"
    item.latest_analyzed_input_revision = item.latest_analyzed_pipeline_version = None
    db_session.commit()
    snapshot = replace(publication._publication_snapshot(identity), group_id=ACTIVE)
    for group in (ACTIVE, "active_b"):
        result = _queue(db_session, replace(snapshot, group_id=group))
        assert result.job.id == job.id
        assert job.status == "running"
        assert item.latest_desired_input_revision == identity.input_revision
        assert item.supersession_requested_at is None
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics,completed")
    assert _queue(db_session, replace(snapshot, group_id="completed")).outcome == "excluded"
    assert job.status == "cancelled"
    assert job.last_error == "completed_folder"
    assert job.next_retry_at is None
    assert item.latest_desired_input_revision is None


def test_unregistered_active_item_is_not_automatically_admitted(db_session):
    identity = ProcessingIdentity("a" * 64, settings.design_processing_pipeline_version)
    result = _queue(db_session, replace(publication._publication_snapshot(identity), group_id=ACTIVE))
    assert result.outcome == "excluded"
    assert result.item is None
    assert db_session.query(DesignProcessingJob).count() == 0


def test_reconciliation_recovers_old_cancelled_active_item_without_reanalysis(db_session, monkeypatch):
    identity, item, job, storage, _ = publication._seed_publication(db_session)
    item.state = "ineligible"
    item.latest_desired_input_revision = item.latest_desired_pipeline_version = None
    item.created_at = utc_now() - timedelta(days=100)
    job.status = "cancelled"
    job.last_error = "item is no longer in the design-processing Landing Zone"
    db_session.commit()
    gateway = publication.PublicationGateway(replace(publication._publication_snapshot(identity), group_id=ACTIVE))
    monkeypatch.setattr(reconciliation, "list_items_in_groups", lambda *args: {"group_mkpbd6vy": []})
    args = dict(access_token="test", gateway=gateway, mode="enabled", activation_timestamp=utc_now(), limit=1)
    assert reconciliation.reconcile_landing_zone_once(db_session, dry_run=True, **args).queued == 1
    assert db_session.query(DesignProcessingJob).count() == 1
    assert item.state == "ineligible"
    assert reconciliation.reconcile_landing_zone_once(db_session, dry_run=False, **args).queued == 1
    assert db_session.query(DesignProcessingJob).count() == 2
    assert publication._run_publication(db_session, gateway, storage).published == 1
    assert job.status == "cancelled"
    assert item.state == "ready_for_review"


def test_reconciliation_cancels_when_completed_move_webhook_was_missed(db_session, monkeypatch):
    identity, item, job, _, _ = publication._seed_publication(db_session)
    gateway = publication.PublicationGateway(replace(publication._publication_snapshot(identity), group_id="completed"))
    monkeypatch.setattr(reconciliation, "list_items_in_groups", lambda *args: {})
    result = reconciliation.reconcile_landing_zone_once(
        db_session, dry_run=False, access_token="test", gateway=gateway,
        mode="enabled", activation_timestamp=utc_now(),
    )
    assert result.excluded == 1
    assert job.status == "cancelled"
    assert item.state == "ineligible"


def test_completed_item_cannot_be_manually_retried(db_session):
    identity, item, job, _, _ = publication._seed_publication(db_session)
    job.status = "failed"
    item.state = "failed"
    db_session.commit()
    gateway = publication.PublicationGateway(replace(publication._publication_snapshot(identity), group_id="completed"))
    result = retry_failed_design_processing_job(db_session, job.id, mode="enabled", gateway=gateway)
    assert result.outcome == "excluded"
    assert job.status == "failed"
    assert item.state == "ineligible"


def test_completed_folder_blocks_old_file_cleanup(db_session):
    prior = ProcessingIdentity("b" * 64, settings.design_processing_pipeline_version)
    identity, item, _, storage, old = publication._seed_publication(db_session, prior_identity=prior)
    gateway = publication.PublicationGateway(publication._publication_snapshot(identity))
    gateway.fail_deletes = True
    assert publication._run_publication(db_session, gateway, storage).published == 1
    gateway.fail_deletes = False
    gateway.events.clear()
    gateway.snapshot = replace(gateway.snapshot, group_id="completed")
    assert cleanup_delete_pending_artifacts(db_session, gateway=gateway) == (0, 0)
    assert not any(event[0] == "delete" for event in gateway.events)
    assert item.state == "ineligible"
    assert all(artifact.status == "delete_pending" for artifact in old)


def test_upload_error_after_completion_is_cancelled_without_retry(db_session):
    identity, item, job, storage, _ = publication._seed_publication(db_session)

    class CompletingGateway(publication.PublicationGateway):
        def upload_design_file(self, *args):
            self.snapshot = replace(self.snapshot, group_id="completed")
            raise RuntimeError("upload interrupted")

    gateway = CompletingGateway(publication._publication_snapshot(identity))
    assert publication._run_publication(db_session, gateway, storage).cancelled == 1
    assert item.state == "ineligible"
    assert job.status == "cancelled"
    assert job.next_retry_at is None


def test_completed_snapshot_does_not_require_readable_email_assets():
    snapshot = parse_design_processing_target({
        "id": "123", "board": {"id": "1882196103"}, "group": {"id": "completed"},
        "name": "Completed enquiry", "state": "active", "assets": None, "column_values": None,
    })
    assert snapshot.group_id == "completed"
    assert snapshot.input_revision is None


def _blocking_child(connection, started_path):
    started_path.write_text("started")
    time.sleep(30)
    connection.send((True, "should never arrive"))


def _returning_child(connection):
    connection.send((True, {"result": "ok"}))


def test_extraction_process_is_terminated_on_cancellation(tmp_path):
    marker = tmp_path / "started"
    deadline = time.monotonic() + 15

    def check():
        if marker.exists():
            raise RuntimeError("completed_folder")
        if time.monotonic() > deadline:
            pytest.fail("child did not start")

    with pytest.raises(RuntimeError, match="completed_folder"):
        _run_in_process(_blocking_child, (marker,), check_current=check, poll_seconds=0.05)
    assert not any(child.name == "design-extraction" for child in multiprocessing.active_children())


def test_extraction_process_returns_result_and_rechecks_eligibility():
    checks = []
    result = _run_in_process(_returning_child, (), check_current=lambda: checks.append(True), poll_seconds=0.05)
    assert result == {"result": "ok"}
    assert checks


def _offline_generate(model, contents, config):
    if config.response_mime_type == "application/json":
        return SimpleNamespace(text=json.dumps({name: None for name in DesignParameterExtraction.model_fields}))
    return SimpleNamespace(text="Test Roof")


def test_production_analysis_process_serializes_client_and_returns_extraction(tmp_path):
    content = b"From: test@example.com\r\nSubject: Test Roof\r\n\r\nRoof enquiry\r\n"
    path = tmp_path / "test.eml"
    path.write_bytes(content)
    asset = DesignEmailAsset(
        asset_id="1", filename="test.eml", file_extension="eml", size=len(content),
        created_at="2026-09-29T00:00:00Z", download_url="unused", download_requires_auth=False,
    )
    downloaded = DownloadedDesignEmailAsset(
        source=asset, temp_path=str(path), content_type="message/rfc822",
        content_sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content),
    )
    result = run_interruptible_analysis(
        (downloaded,), client=LegacyGeminiClient("offline", generate_content=_offline_generate),
        check_current=lambda: None, poll_seconds=0.05,
    )
    assert result.project_name == "Test Roof"
    assert result.parameters["Reason for Change"] == "Reviewer decision required"
