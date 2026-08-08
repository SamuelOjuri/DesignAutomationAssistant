from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import monday_client
from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import (
    DesignProcessingArtifact,
    DesignProcessingItem,
    DesignProcessingJob,
)
from backend.app.services.auto_sync import utc_now
from backend.app.services.design_processing_artifacts import (
    build_artifact_object_key,
    deterministic_artifact_filenames,
)
from backend.app.services.design_processing_inputs import (
    DesignEmailAsset,
    DesignProcessingTargetSnapshot,
)
from backend.app.services.design_processing_pipeline import (
    build_design_owned_column_values,
    cleanup_delete_pending_artifacts,
)
from backend.app.services.design_processing_state import (
    ProcessingIdentity,
    is_ready_for_review,
)
from backend.app.services.design_processing_worker import run_worker_once


@pytest.mark.parametrize(
    "column_id",
    [
        "dropdown_mkpb98es",
        "board_relation_mkpbm5np",
        "board_relation_mm3c4g5x",
        "name",
        "file_mkpbm883",
        "lookup_mkpb44am",
        "unknown",
    ],
)
def test_scalar_helper_rejects_non_owned_columns_before_transport(
    monkeypatch,
    column_id,
):
    calls = []
    monkeypatch.setattr(
        monday_client,
        "monday_graphql_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(monday_client.MondayWriteContractError):
        monday_client.update_design_owned_columns(
            "token",
            "1882196103",
            "123",
            {column_id: "value"},
        )

    assert calls == []


@pytest.mark.parametrize("invalid_value", [None, ""])
def test_scalar_helper_refuses_to_clear_owned_columns(monkeypatch, invalid_value):
    calls = []
    monkeypatch.setattr(
        monday_client,
        "monday_graphql_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(monday_client.MondayWriteContractError):
        monday_client.update_design_owned_columns(
            "token",
            "1882196103",
            "123",
            {"date_mkpb23av": invalid_value},
        )

    assert calls == []


def test_scalar_helper_sends_only_owned_columns(monkeypatch):
    calls = []

    def request(token, query, variables, *, timeout):
        calls.append((token, query, variables, timeout))
        return {"data": {"change_multiple_column_values": {"id": "123"}}}

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)

    monday_client.update_design_owned_columns(
        "token",
        "1882196103",
        "123",
        {
            "date_mkpb23av": {"date": "2026-08-07"},
            "hour_mkpbb3j1": {"hour": 10, "minute": 30},
            "dropdown_mkpbafca": {"ids": [4]},
        },
    )

    assert len(calls) == 1
    assert set(json.loads(calls[0][2]["columnValues"])) == {
        "date_mkpb23av",
        "hour_mkpbb3j1",
        "dropdown_mkpbafca",
    }


@pytest.mark.parametrize(
    "column_id",
    ["file_mkpbm883", "name", "dropdown_mkpb98es", "unknown"],
)
def test_file_helpers_reject_non_owned_columns_before_transport(
    monkeypatch,
    column_id,
):
    post_calls = []
    graphql_calls = []
    monkeypatch.setattr(
        monday_client.requests,
        "post",
        lambda *args, **kwargs: post_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        monday_client,
        "monday_graphql_request",
        lambda *args, **kwargs: graphql_calls.append((args, kwargs)),
    )

    with pytest.raises(monday_client.MondayWriteContractError):
        monday_client.upload_design_file(
            "token",
            "123",
            column_id,
            "artifact.csv",
            b"content",
            "text/csv",
        )
    with pytest.raises(monday_client.MondayWriteContractError):
        monday_client.delete_design_file(
            "token",
            "1882196103",
            "123",
            column_id,
            "456",
        )

    assert post_calls == []
    assert graphql_calls == []


def test_delete_helper_targets_one_recorded_asset(monkeypatch):
    calls = []

    def request(token, query, variables, *, timeout):
        calls.append((token, query, variables, timeout))
        return {"data": {"update_assets_on_item": {"id": "123"}}}

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    monkeypatch.setattr(
        monday_client,
        "inspect_design_processing_file_columns",
        lambda token, item_id: {
            "file_mkza7y37": (),
            "file_mm59rntf": (
                monday_client.MondayFileColumnAsset(
                    asset_id="456",
                    filename="old.pdf",
                    size_bytes=10,
                    created_at=None,
                ),
                monday_client.MondayFileColumnAsset(
                    asset_id="789",
                    filename="replacement.pdf",
                    size_bytes=20,
                    created_at=None,
                ),
                monday_client.MondayFileColumnAsset(
                    asset_id="790",
                    filename="retained.pdf",
                    size_bytes=30,
                    created_at=None,
                ),
            ),
        },
    )

    monday_client.delete_design_file(
        "token",
        "1882196103",
        "123",
        "file_mm59rntf",
        "456",
    )

    assert len(calls) == 1
    assert calls[0][2] == {
        "boardId": "1882196103",
        "itemId": "123",
        "columnId": "file_mm59rntf",
        "files": [
            {
                "assetId": "789",
                "fileType": "asset",
                "name": "replacement.pdf",
            },
            {
                "assetId": "790",
                "fileType": "asset",
                "name": "retained.pdf",
            },
        ],
    }
    assert "clear" not in calls[0][1].lower()


def test_delete_helper_refuses_to_remove_final_file(monkeypatch):
    calls = []
    target = monday_client.MondayFileColumnAsset(
        asset_id="456",
        filename="old.pdf",
        size_bytes=10,
        created_at=None,
    )
    monkeypatch.setattr(
        monday_client,
        "inspect_design_processing_file_columns",
        lambda token, item_id: {
            "file_mkza7y37": (),
            "file_mm59rntf": (target,),
        },
    )
    monkeypatch.setattr(
        monday_client,
        "monday_graphql_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(
        monday_client.MondayWriteContractError,
        match="cannot remove the final file",
    ):
        monday_client.delete_design_file(
            "token",
            "1882196103",
            "123",
            "file_mm59rntf",
            "456",
        )

    assert calls == []


def test_delete_helper_treats_missing_recorded_asset_as_already_removed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        monday_client,
        "inspect_design_processing_file_columns",
        lambda token, item_id: {
            "file_mkza7y37": (),
            "file_mm59rntf": (
                monday_client.MondayFileColumnAsset(
                    asset_id="789",
                    filename="replacement.pdf",
                    size_bytes=20,
                    created_at=None,
                ),
            ),
        },
    )
    monkeypatch.setattr(
        monday_client,
        "monday_graphql_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    monday_client.delete_design_file(
        "token",
        "1882196103",
        "123",
        "file_mm59rntf",
        "456",
    )

    assert calls == []


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


class MemoryArtifactStorage:
    def __init__(self):
        self.objects = {}

    def write_private(self, bucket, object_key, content, content_type):
        self.objects[(bucket, object_key)] = bytes(content)

    def read_private(self, bucket, object_key):
        return self.objects[(bucket, object_key)]


class PublicationGateway:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.events = []
        self.assets = {
            "file_mkza7y37": [],
            "file_mm59rntf": [],
        }
        self.next_asset_id = 1000
        self.fail_upload_after_store = {}
        self.fail_deletes = False

    def fetch_target(self, item_id):
        assert item_id == self.snapshot.item_id
        self.events.append(("gate", item_id))
        return self.snapshot

    def inspect_file_columns(self, item_id):
        assert item_id == self.snapshot.item_id
        self.events.append(("inspect", item_id))
        return {
            column_id: tuple(assets)
            for column_id, assets in self.assets.items()
        }

    def fetch_design_owned_column_settings(self, board_id):
        self.events.append(("settings", board_id))
        return {
            "date_mkpb23av": "{}",
            "hour_mkpbb3j1": "{}",
            "dropdown_mkpbafca": json.dumps(
                {"labels": [{"id": 9, "name": "SW2 3AA"}]}
            ),
        }

    def update_design_owned_columns(self, board_id, item_id, column_values):
        self.events.append(("update", dict(column_values)))

    def upload_design_file(
        self,
        item_id,
        column_id,
        filename,
        content,
        content_type,
    ):
        self.events.append(("upload", column_id, filename))
        self.next_asset_id += 1
        asset = monday_client.MondayFileColumnAsset(
            asset_id=str(self.next_asset_id),
            filename=filename,
            size_bytes=len(content),
            created_at="2026-08-07T12:00:00Z",
        )
        self.assets[column_id].append(asset)
        failures = self.fail_upload_after_store.get(column_id, 0)
        if failures:
            self.fail_upload_after_store[column_id] = failures - 1
            raise RuntimeError("simulated uncertain upload outcome")
        return asset

    def delete_design_file(self, board_id, item_id, column_id, asset_id):
        self.events.append(("delete", column_id, asset_id))
        if self.fail_deletes:
            raise RuntimeError("simulated cleanup failure")


def _publication_snapshot(identity):
    asset = DesignEmailAsset(
        asset_id="700",
        filename="enquiry.msg",
        file_extension="msg",
        size=123,
        created_at="2026-08-07T10:00:00Z",
        download_url="https://monday.invalid/700",
        download_requires_auth=True,
    )
    return DesignProcessingTargetSnapshot(
        board_id="1882196103",
        item_id="123",
        group_id="group_mkpbd6vy",
        name="Human entered name",
        email_assets=(asset,),
        input_revision=identity.input_revision,
    )


def _seed_publication(db, *, prior_identity=None):
    now = utc_now()
    identity = ProcessingIdentity(
        "a" * 64,
        settings.design_processing_pipeline_version,
    )
    item = DesignProcessingItem(
        id=uuid.uuid4(),
        board_id="1882196103",
        item_id="123",
        latest_desired_input_revision=identity.input_revision,
        latest_desired_pipeline_version=identity.pipeline_version,
        latest_analyzed_input_revision=identity.input_revision,
        latest_analyzed_pipeline_version=identity.pipeline_version,
        latest_published_input_revision=(
            prior_identity.input_revision if prior_identity else None
        ),
        latest_published_pipeline_version=(
            prior_identity.pipeline_version if prior_identity else None
        ),
        state="analyzed",
        extracted_parameters_json={
            "schemaVersion": 1,
            "inputRevision": identity.input_revision,
            "pipelineVersion": identity.pipeline_version,
            "parameters": {
                "Date Received": "07/08/2026",
                "Hour Received": "10:30",
                "Post Code": "SW2 3AA",
            },
            "sources": {},
        },
        match_result_json={
            "schemaVersion": 1,
            "inputRevision": identity.input_revision,
            "pipelineVersion": identity.pipeline_version,
            "result": {"schemaVersion": 1, "candidateCount": 0, "candidates": []},
        },
        warnings_json=[],
        created_at=now,
        updated_at=now,
    )
    job = DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=item.board_id,
        item_id=item.item_id,
        trigger_type="phase6_test",
        status="scheduled",
        scheduled_for=now,
        attempt_count=0,
        readiness_check_count=0,
        max_attempts=3,
        created_at=now,
        updated_at=now,
    )
    storage = MemoryArtifactStorage()
    csv_filename, pdf_filename = deterministic_artifact_filenames(item.item_id, identity)
    for kind, column_id, filename, content in (
        ("ai_data", "file_mkza7y37", csv_filename, b"csv-content"),
        ("match_report", "file_mm59rntf", pdf_filename, b"pdf-content"),
    ):
        object_key = build_artifact_object_key(
            board_id=item.board_id,
            item_id=item.item_id,
            identity=identity,
            filename=filename,
        )
        storage.objects[("test-bucket", object_key)] = content
        db.add(
            DesignProcessingArtifact(
                id=uuid.uuid4(),
                board_id=item.board_id,
                item_id=item.item_id,
                column_id=column_id,
                artifact_kind=kind,
                input_revision=identity.input_revision,
                pipeline_version=identity.pipeline_version,
                deterministic_filename=filename,
                storage_bucket="test-bucket",
                storage_object_key=object_key,
                content_sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                status="rendered",
                created_at=now,
                updated_at=now,
            )
        )
    prior_artifacts = []
    if prior_identity is not None:
        for kind, column_id, suffix, asset_id in (
            ("ai_data", "file_mkza7y37", "csv", "801"),
            ("match_report", "file_mm59rntf", "pdf", "802"),
        ):
            artifact = DesignProcessingArtifact(
                id=uuid.uuid4(),
                board_id=item.board_id,
                item_id=item.item_id,
                column_id=column_id,
                artifact_kind=kind,
                input_revision=prior_identity.input_revision,
                pipeline_version=prior_identity.pipeline_version,
                deterministic_filename=f"prior.{suffix}",
                storage_bucket="test-bucket",
                storage_object_key=f"prior/{kind}",
                content_sha256="b" * 64,
                size_bytes=1,
                monday_asset_id=asset_id,
                status="published",
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(days=1),
            )
            prior_artifacts.append(artifact)
            db.add(artifact)
    db.add_all([item, job])
    db.commit()
    return identity, item, job, storage, prior_artifacts


def _run_publication(db, gateway, storage):
    return run_worker_once(
        db,
        worker_id="phase6-worker",
        access_token="test-token",
        gateway=gateway,
        analysis_client=object(),
        artifact_storage=storage,
        mode="enabled",
        claim_limit=1,
        recover_leases=False,
        heartbeat_interval_seconds=0,
    )


def test_invalid_scalar_values_are_omitted_without_clearing():
    values, warnings = build_design_owned_column_values(
        {
            "Date Received": "Not found",
            "Hour Received": "invalid",
            "Post Code": "UNMAPPED",
        },
        {
            "date_mkpb23av": "{}",
            "hour_mkpbb3j1": "{}",
            "dropdown_mkpbafca": json.dumps({"labels": []}),
        },
    )

    assert values == {}
    assert len(warnings) == 3


def test_not_provided_post_code_is_omitted_without_clearing():
    values, warnings = build_design_owned_column_values(
        {
            "Date Received": "07 Feb 2025",
            "Hour Received": "13:34",
            "Post Code": "Not provided",
        },
        {
            "date_mkpb23av": "{}",
            "hour_mkpbb3j1": "{}",
            "dropdown_mkpbafca": json.dumps(
                {"labels": [{"id": 9, "name": "SW2 3AA"}]}
            ),
        },
    )

    assert values == {
        "date_mkpb23av": {"date": "2025-02-07"},
        "hour_mkpbb3j1": {"hour": 13, "minute": 34},
    }
    assert warnings == ("Zip Code was not written because it is missing or unmapped",)


def test_publication_gates_each_side_effect_and_advances_atomically(db_session):
    identity, _, _, storage, _ = _seed_publication(db_session)
    gateway = PublicationGateway(_publication_snapshot(identity))

    result = _run_publication(db_session, gateway, storage)

    item = db_session.query(DesignProcessingItem).one()
    job = db_session.query(DesignProcessingJob).one()
    artifacts = db_session.query(DesignProcessingArtifact).all()
    assert result.published == 1
    assert job.status == "completed"
    assert item.state == "ready_for_review"
    assert (
        item.latest_published_input_revision,
        item.latest_published_pipeline_version,
    ) == (identity.input_revision, identity.pipeline_version)
    assert all(
        artifact.status == "published" and artifact.monday_asset_id
        for artifact in artifacts
    )
    side_effect_indexes = [
        index
        for index, event in enumerate(gateway.events)
        if event[0] in {"update", "upload", "delete"}
    ]
    assert side_effect_indexes
    assert all(gateway.events[index - 1][0] == "gate" for index in side_effect_indexes)
    update = next(event for event in gateway.events if event[0] == "update")
    assert set(update[1]) == {
        "date_mkpb23av",
        "hour_mkpbb3j1",
        "dropdown_mkpbafca",
    }


def test_pipeline_omits_invalid_values_and_persists_warnings(db_session):
    identity, item, _, storage, _ = _seed_publication(db_session)
    item.extracted_parameters_json = {
        **item.extracted_parameters_json,
        "parameters": {
            "Date Received": "Not found",
            "Hour Received": "invalid",
            "Post Code": "UNMAPPED",
        },
    }
    db_session.commit()
    gateway = PublicationGateway(_publication_snapshot(identity))

    result = _run_publication(db_session, gateway, storage)

    item = db_session.query(DesignProcessingItem).one()
    assert result.published == 1
    assert not [event for event in gateway.events if event[0] == "update"]
    assert len(item.warnings_json) == 3


def test_uncertain_upload_is_adopted_without_repeating_analysis(db_session):
    identity, _, job, storage, _ = _seed_publication(db_session)
    gateway = PublicationGateway(_publication_snapshot(identity))
    gateway.fail_upload_after_store["file_mkza7y37"] = 1

    first = _run_publication(db_session, gateway, storage)

    assert first.retry_wait == 1
    ai_artifact = db_session.query(DesignProcessingArtifact).filter_by(
        artifact_kind="ai_data"
    ).one()
    assert ai_artifact.status == "uploading"
    job = db_session.get(DesignProcessingJob, job.id)
    job.scheduled_for = utc_now() - timedelta(seconds=1)
    job.next_retry_at = job.scheduled_for
    db_session.commit()

    second = _run_publication(db_session, gateway, storage)

    assert second.published == 1
    ai_uploads = [
        event
        for event in gateway.events
        if event[:2] == ("upload", "file_mkza7y37")
    ]
    assert len(ai_uploads) == 1
    assert db_session.query(DesignProcessingJob).one().attempt_count == 2


def test_ambiguous_adoption_retries_without_uploading(db_session):
    identity, _, _, storage, _ = _seed_publication(db_session)
    gateway = PublicationGateway(_publication_snapshot(identity))
    artifact = db_session.query(DesignProcessingArtifact).filter_by(
        artifact_kind="ai_data"
    ).one()
    gateway.assets[artifact.column_id] = [
        monday_client.MondayFileColumnAsset(
            asset_id=str(asset_id),
            filename=artifact.deterministic_filename,
            size_bytes=artifact.size_bytes,
            created_at=None,
        )
        for asset_id in (901, 902)
    ]

    result = _run_publication(db_session, gateway, storage)

    assert result.retry_wait == 1
    assert not [event for event in gateway.events if event[0] == "upload"]
    job = db_session.query(DesignProcessingJob).one()
    assert "multiple Monday assets" in job.last_error


def test_same_name_wrong_size_asset_is_not_adopted(db_session):
    identity, _, _, storage, _ = _seed_publication(db_session)
    gateway = PublicationGateway(_publication_snapshot(identity))
    artifact = db_session.query(DesignProcessingArtifact).filter_by(
        artifact_kind="ai_data"
    ).one()
    gateway.assets[artifact.column_id] = [
        monday_client.MondayFileColumnAsset(
            asset_id="901",
            filename=artifact.deterministic_filename,
            size_bytes=artifact.size_bytes + 1,
            created_at=None,
        )
    ]

    result = _run_publication(db_session, gateway, storage)

    assert result.retry_wait == 1
    assert not [event for event in gateway.events if event[0] == "upload"]
    assert "wrong size" in db_session.query(DesignProcessingJob).one().last_error


def test_report_retry_does_not_repeat_columns_or_ai_upload(db_session):
    identity, _, job, storage, _ = _seed_publication(db_session)
    gateway = PublicationGateway(_publication_snapshot(identity))
    gateway.fail_upload_after_store["file_mm59rntf"] = 1

    first = _run_publication(db_session, gateway, storage)

    item = db_session.query(DesignProcessingItem).one()
    artifacts = {
        artifact.artifact_kind: artifact
        for artifact in db_session.query(DesignProcessingArtifact).all()
    }
    assert first.retry_wait == 1
    assert item.latest_published_input_revision is None
    assert item.state == "publishing"
    assert artifacts["ai_data"].status == "published"
    assert artifacts["match_report"].status == "uploading"
    job = db_session.get(DesignProcessingJob, job.id)
    job.scheduled_for = utc_now() - timedelta(seconds=1)
    job.next_retry_at = job.scheduled_for
    db_session.commit()

    second = _run_publication(db_session, gateway, storage)

    assert second.published == 1
    assert len([event for event in gateway.events if event[0] == "update"]) == 1
    assert len(
        [
            event
            for event in gateway.events
            if event[:2] == ("upload", "file_mkza7y37")
        ]
    ) == 1
    assert len(
        [
            event
            for event in gateway.events
            if event[:2] == ("upload", "file_mm59rntf")
        ]
    ) == 1
    item = db_session.query(DesignProcessingItem).one()
    assert item.latest_published_input_revision == identity.input_revision
    assert item.state == "ready_for_review"


def test_publication_retry_cancels_when_mode_no_longer_allows_writes(db_session):
    identity, _, job, storage, _ = _seed_publication(db_session)
    gateway = PublicationGateway(_publication_snapshot(identity))
    gateway.fail_upload_after_store["file_mm59rntf"] = 1
    assert _run_publication(db_session, gateway, storage).retry_wait == 1
    job = db_session.get(DesignProcessingJob, job.id)
    job.scheduled_for = utc_now() - timedelta(seconds=1)
    job.next_retry_at = job.scheduled_for
    db_session.commit()
    event_count = len(gateway.events)

    result = run_worker_once(
        db_session,
        worker_id="phase6-worker",
        access_token="test-token",
        gateway=gateway,
        analysis_client=object(),
        artifact_storage=storage,
        mode="shadow",
        claim_limit=1,
        recover_leases=False,
        heartbeat_interval_seconds=0,
    )

    item = db_session.query(DesignProcessingItem).one()
    job = db_session.query(DesignProcessingJob).one()
    assert result.cancelled == 1
    assert job.status == "cancelled"
    assert item.latest_published_input_revision is None
    assert item.state == "analyzed"
    assert len(gateway.events) == event_count


def test_publication_rechecks_policy_before_each_file_upload(db_session):
    identity, _, _, storage, _ = _seed_publication(db_session)
    publication_allowed = True

    class RevokingGateway(PublicationGateway):
        def update_design_owned_columns(self, board_id, item_id, column_values):
            nonlocal publication_allowed
            super().update_design_owned_columns(board_id, item_id, column_values)
            publication_allowed = False

    gateway = RevokingGateway(_publication_snapshot(identity))

    result = run_worker_once(
        db_session,
        worker_id="phase7-worker",
        access_token="test-token",
        gateway=gateway,
        analysis_client=object(),
        artifact_storage=storage,
        mode="enabled",
        execution_policy=lambda kind, item_id: (
            True if kind == "analysis" else publication_allowed
        ),
        claim_limit=1,
        recover_leases=False,
        heartbeat_interval_seconds=0,
    )

    item = db_session.query(DesignProcessingItem).one()
    job = db_session.query(DesignProcessingJob).one()
    assert result.cancelled == 1
    assert job.status == "cancelled"
    assert item.state == "analyzed"
    assert not [event for event in gateway.events if event[0] == "upload"]


def test_publication_rechecks_policy_before_advancing_published_identity(db_session):
    identity, _, _, storage, _ = _seed_publication(db_session)
    publication_allowed = True

    class FinalUploadRevokingGateway(PublicationGateway):
        def upload_design_file(
            self,
            item_id,
            column_id,
            filename,
            content,
            content_type,
        ):
            nonlocal publication_allowed
            asset = super().upload_design_file(
                item_id,
                column_id,
                filename,
                content,
                content_type,
            )
            if column_id == "file_mm59rntf":
                publication_allowed = False
            return asset

    gateway = FinalUploadRevokingGateway(_publication_snapshot(identity))
    result = run_worker_once(
        db_session,
        worker_id="phase7-worker",
        access_token="test-token",
        gateway=gateway,
        analysis_client=object(),
        artifact_storage=storage,
        mode="enabled",
        execution_policy=lambda kind, item_id: (
            True if kind == "analysis" else publication_allowed
        ),
        claim_limit=1,
        recover_leases=False,
        heartbeat_interval_seconds=0,
    )

    item = db_session.query(DesignProcessingItem).one()
    job = db_session.query(DesignProcessingJob).one()
    assert result.cancelled == 1
    assert job.status == "cancelled"
    assert item.state == "analyzed"
    assert item.latest_published_input_revision is None
    assert all(
        artifact.status == "published" and artifact.monday_asset_id is not None
        for artifact in db_session.query(DesignProcessingArtifact).all()
    )


def test_revision_change_after_ai_upload_blocks_final_report(db_session):
    identity, _, _, storage, _ = _seed_publication(db_session)

    class RevisionChangingGateway(PublicationGateway):
        def upload_design_file(
            self,
            item_id,
            column_id,
            filename,
            content,
            content_type,
        ):
            asset = super().upload_design_file(
                item_id,
                column_id,
                filename,
                content,
                content_type,
            )
            if column_id == "file_mkza7y37":
                self.snapshot = replace(
                    self.snapshot,
                    input_revision="b" * 64,
                )
            return asset

    gateway = RevisionChangingGateway(_publication_snapshot(identity))

    result = _run_publication(db_session, gateway, storage)

    item = db_session.query(DesignProcessingItem).one()
    jobs = db_session.query(DesignProcessingJob).all()
    artifacts = {
        artifact.artifact_kind: artifact
        for artifact in db_session.query(DesignProcessingArtifact).filter_by(
            input_revision=identity.input_revision
        )
    }
    assert result.cancelled == 1
    assert item.latest_desired_input_revision == "b" * 64
    assert item.latest_published_input_revision is None
    assert item.state != "ready_for_review"
    assert artifacts["ai_data"].status == "published"
    assert artifacts["match_report"].monday_asset_id is None
    assert len(
        [
            event
            for event in gateway.events
            if event[:2] == ("upload", "file_mm59rntf")
        ]
    ) == 0
    assert len([job for job in jobs if job.status == "scheduled"]) == 1
    assert len([job for job in jobs if job.status == "cancelled"]) == 1


def test_replacement_is_published_before_prior_assets_are_deleted(db_session):
    prior_identity = ProcessingIdentity(
        "c" * 64,
        settings.design_processing_pipeline_version,
    )
    identity, _, _, storage, prior = _seed_publication(
        db_session,
        prior_identity=prior_identity,
    )
    gateway = PublicationGateway(_publication_snapshot(identity))

    result = _run_publication(db_session, gateway, storage)

    assert result.published == 1
    uploads = [index for index, event in enumerate(gateway.events) if event[0] == "upload"]
    deletes = [index for index, event in enumerate(gateway.events) if event[0] == "delete"]
    assert uploads and deletes
    assert max(uploads) < min(deletes)
    assert all(
        db_session.get(DesignProcessingArtifact, artifact.id).status == "deleted"
        for artifact in prior
    )


def test_cleanup_failure_does_not_undo_successful_publication(db_session):
    prior_identity = ProcessingIdentity(
        "c" * 64,
        settings.design_processing_pipeline_version,
    )
    identity, _, _, storage, prior = _seed_publication(
        db_session,
        prior_identity=prior_identity,
    )
    gateway = PublicationGateway(_publication_snapshot(identity))
    gateway.fail_deletes = True

    result = _run_publication(db_session, gateway, storage)

    item = db_session.query(DesignProcessingItem).one()
    assert result.published == 1
    assert item.state == "ready_for_review"
    assert item.latest_published_input_revision == identity.input_revision
    assert all(
        db_session.get(DesignProcessingArtifact, artifact.id).status
        == "delete_pending"
        for artifact in prior
    )
    assert all(
        db_session.get(DesignProcessingArtifact, artifact.id).last_error
        for artifact in prior
    )


def test_cleanup_retries_after_reviewer_moves_item(db_session):
    prior_identity = ProcessingIdentity(
        "c" * 64,
        settings.design_processing_pipeline_version,
    )
    identity, _, _, storage, prior = _seed_publication(
        db_session,
        prior_identity=prior_identity,
    )
    gateway = PublicationGateway(_publication_snapshot(identity))
    gateway.fail_deletes = True
    assert _run_publication(db_session, gateway, storage).published == 1
    gateway.fail_deletes = False
    gateway.snapshot = replace(gateway.snapshot, group_id="reviewed")
    gate_count = len([event for event in gateway.events if event[0] == "gate"])

    deleted, failed = cleanup_delete_pending_artifacts(
        db_session,
        gateway=gateway,
    )

    assert (deleted, failed) == (2, 0)
    assert len([event for event in gateway.events if event[0] == "gate"]) == gate_count
    assert all(
        db_session.get(DesignProcessingArtifact, artifact.id).status == "deleted"
        for artifact in prior
    )


def test_ready_for_review_requires_both_recorded_asset_ids(db_session):
    identity, item, _, _, _ = _seed_publication(db_session)
    item.latest_published_input_revision = identity.input_revision
    item.latest_published_pipeline_version = identity.pipeline_version
    artifacts = db_session.query(DesignProcessingArtifact).all()
    for artifact in artifacts:
        artifact.status = "published"
        artifact.monday_asset_id = (
            "1001" if artifact.artifact_kind == "ai_data" else None
        )
    db_session.flush()

    assert is_ready_for_review(item, artifacts) is False

    next(
        artifact
        for artifact in artifacts
        if artifact.artifact_kind == "match_report"
    ).monday_asset_id = "1002"
    assert is_ready_for_review(item, artifacts) is True