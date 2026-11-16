"""Only admitted ZIPs can replace an absent email as extraction inputs."""

from copy import deepcopy
from datetime import timedelta
import json

import pytest

from backend.app.config import settings
from backend.app.services.design_processing_inputs import parse_design_processing_target
from backend.app.services.design_processing_state import ProcessingIdentity
from backend.app.services.design_processing_target import (
    DesignProcessingTargetMismatch, assert_current_execution_target, refresh_current_target,
)
from backend.tests.test_design_processing_phase3 import (
    NOW, FakeReadGateway, _asset, _item, _processing_item, _running_job,
)
from backend.tests.test_design_processing_phase4 import db_session, _queue


BOARD = "1882196103"
LANDING = "group_mkpbd6vy"
PIPELINE = "pipeline-v1"


def zip_target(*, filename="roof-plans.zip", extension="zip", name="Roof enquiry"):
    asset = _asset("2", filename)
    asset["file_extension"] = extension
    return parse_design_processing_target(_item(
        files=[{"assetId": "2", "name": filename}], assets=[asset], name=name,
    ))


@pytest.mark.parametrize("filename,extension", [
    ("roof-plans.zip", "zip"), ("roof-plans.ZIP", "eml"), ("downloaded-roof-plans", ".ZIP"),
])
def test_admitted_zip_has_inputs_without_claiming_email_presence(monkeypatch, filename, extension):
    monkeypatch.setattr(settings, "design_processing_source_file_column_ids", [])
    target = zip_target(filename=filename, extension=extension)
    assert target.missing_email and not target.email_assets
    assert not target.missing_inputs and target.input_revision is not None
    assert [asset.filename for asset in target.supplementary_assets] == [filename]


def test_zip_only_revision_is_stable_but_tracks_replacement_and_email_arrival(monkeypatch):
    monkeypatch.setattr(settings, "design_processing_source_file_column_ids", [])
    raw = _item(files=[{"assetId": "2"}, {"assetId": "3"}],
                assets=[_asset("2", "first.zip"), _asset("3", "second.zip")])
    target = parse_design_processing_target(raw)
    reordered = deepcopy(raw)
    reordered["assets"].reverse()
    reordered["assets"][0]["url"] = "https://monday.invalid/rotated-download"
    reordered["name"] = "Renamed by reviewer"
    reordered["column_values"][0]["value"] = json.dumps({"files": [{"assetId": "3"}, {"assetId": "2"}]})
    assert parse_design_processing_target(reordered).input_revision == target.input_revision

    replaced = deepcopy(raw)
    replaced["assets"][0]["file_size"] += 1
    assert parse_design_processing_target(replaced).input_revision != target.input_revision
    with_email = deepcopy(raw)
    with_email["assets"].append(_asset("4", "request.eml"))
    with_email["column_values"][0]["value"] = json.dumps({
        "files": [{"assetId": "2"}, {"assetId": "3"}, {"assetId": "4"}],
    })
    updated = parse_design_processing_target(with_email)
    assert not updated.missing_email and not updated.missing_inputs
    assert updated.input_revision != target.input_revision


@pytest.mark.parametrize("filename,configured,has_inputs", [
    ("drawings.zip", True, True), ("drawings.zip", False, False),
    ("drawing.pdf", True, False), ("export.bin", True, False),
])
def test_only_admitted_zip_in_configured_source_columns_unlocks_file_only_work(
        monkeypatch, filename, configured, has_inputs):
    monkeypatch.setattr(settings, "design_processing_source_file_column_ids", ["source_files"] if configured else [])
    raw = _item(assets=[_asset("2", filename)])
    raw["column_values"].append({"id": "source_files", "type": "file",
                                 "value": json.dumps({"files": [{"assetId": "2", "name": filename}]})})
    target = parse_design_processing_target(raw)
    assert target.missing_email
    assert target.missing_inputs is not has_inputs
    assert (target.input_revision is not None) is has_inputs


def test_waiting_item_and_unassigned_job_wake_when_zip_arrives(db_session, monkeypatch):
    monkeypatch.setattr(settings, "design_processing_source_file_column_ids", [])
    waiting = _queue(db_session, parse_design_processing_target(_item()))
    assert waiting.readiness == "waiting_for_email" and waiting.job.attempt_count == 0
    waiting.job.status = "retry_wait"
    waiting.job.scheduled_for = NOW + timedelta(hours=1)
    waiting.job.next_retry_at = waiting.job.scheduled_for
    db_session.commit()

    snapshot = zip_target()
    admitted = _queue(db_session, snapshot)
    db_session.commit()
    assert admitted.readiness == "ready" and admitted.outcome == "coalesced"
    assert admitted.job.id == waiting.job.id and admitted.job.status == "scheduled"
    assert admitted.job.stage is None and admitted.job.next_retry_at is None
    assert admitted.job.attempt_count == 0
    assert admitted.item.latest_desired_input_revision == snapshot.input_revision
    assert admitted.item.latest_desired_pipeline_version == PIPELINE


def test_zip_without_name_still_waits_for_name(db_session):
    result = _queue(db_session, zip_target(name=""))
    assert result.readiness == "waiting_for_name" and result.item.latest_desired_input_revision is None


def test_zip_only_execution_passes_live_readiness_and_retains_input_fencing():
    snapshot = zip_target()
    identity = ProcessingIdentity(snapshot.input_revision, PIPELINE)
    item, job = _processing_item(desired=identity), _running_job(identity)
    assert assert_current_execution_target(item, job, gateway=FakeReadGateway(snapshot),
        pipeline_version=PIPELINE, expected_board_id=BOARD, expected_group_id=LANDING,
        worker_id="worker-1") is snapshot

    changed = zip_target(filename="replacement.zip")
    with pytest.raises(DesignProcessingTargetMismatch) as mismatch:
        assert_current_execution_target(item, job, gateway=FakeReadGateway(changed),
            pipeline_version=PIPELINE, expected_board_id=BOARD, expected_group_id=LANDING,
            worker_id="worker-1")
    assert mismatch.value.reason == "input_changed"


def test_removing_last_zip_restores_waiting_without_fabricating_an_input_identity():
    snapshot = zip_target()
    identity = ProcessingIdentity(snapshot.input_revision, PIPELINE)
    item = _processing_item(desired=identity)
    refreshed = refresh_current_target(item, gateway=FakeReadGateway(parse_design_processing_target(_item())),
        pipeline_version=PIPELINE, expected_board_id=BOARD, expected_group_id=LANDING, now=NOW)
    assert refreshed.readiness == "waiting_for_email"
    assert item.latest_desired_input_revision is None and item.latest_desired_pipeline_version is None
