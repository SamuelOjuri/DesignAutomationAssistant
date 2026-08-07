from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import logging
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import (
    DesignProcessingArtifact,
    DesignProcessingItem,
    DesignProcessingJob,
    MondayWebhookDispatch,
    MondayWebhookEvent,
)
from backend.app.services.auto_sync import utc_now
from backend.app.services.design_processing_inputs import (
    DesignEmailAsset,
    DesignProcessingTargetSnapshot,
)
from backend.app.services.design_processing_observability import (
    collect_design_processing_metrics,
    log_design_processing_event,
)
from backend.app.services.design_processing_operations import (
    enqueue_design_processing_item,
    retry_design_processing_cleanup,
    retry_failed_design_processing_job,
)
from backend.app.services.design_processing_state import ProcessingIdentity
from backend.app.services.design_processing_worker import run_worker_once


BOARD_ID = "1882196103"
GROUP_ID = "group_mkpbd6vy"


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


def _identity(revision: str = "a" * 64) -> ProcessingIdentity:
    return ProcessingIdentity(
        revision,
        settings.design_processing_pipeline_version,
    )


def _snapshot(item_id: str = "123") -> DesignProcessingTargetSnapshot:
    identity = _identity()
    return DesignProcessingTargetSnapshot(
        board_id=BOARD_ID,
        item_id=item_id,
        group_id=GROUP_ID,
        name="Human entered name",
        email_assets=(
            DesignEmailAsset(
                asset_id="700",
                filename="enquiry.msg",
                file_extension="msg",
                size=100,
                created_at="2026-08-07T10:00:00Z",
                download_url="https://monday.invalid/700",
                download_requires_auth=True,
            ),
        ),
        input_revision=identity.input_revision,
    )


class SnapshotGateway:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def fetch_target(self, item_id):
        assert item_id == self.snapshot.item_id
        return self.snapshot


class FailingSnapshotGateway:
    def fetch_target(self, item_id):
        raise AssertionError("off mode attempted a Monday read")


def _item(item_id: str, identity: ProcessingIdentity, *, state: str):
    now = utc_now()
    return DesignProcessingItem(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=item_id,
        latest_desired_input_revision=identity.input_revision,
        latest_desired_pipeline_version=identity.pipeline_version,
        latest_analyzed_input_revision=(
            identity.input_revision if state == "analyzed" else None
        ),
        latest_analyzed_pipeline_version=(
            identity.pipeline_version if state == "analyzed" else None
        ),
        state=state,
        warnings_json=[],
        created_at=now,
        updated_at=now,
    )


def test_operator_enqueue_is_idempotent(db_session):
    gateway = SnapshotGateway(_snapshot())

    first = enqueue_design_processing_item(
        db_session,
        "123",
        gateway=gateway,
        mode="enabled",
    )
    second = enqueue_design_processing_item(
        db_session,
        "123",
        gateway=gateway,
        mode="enabled",
    )

    assert first["outcome"] == "queued"
    assert first["createdJob"] is True
    assert second["outcome"] == "coalesced"
    assert second["jobId"] == first["jobId"]
    assert db_session.query(DesignProcessingJob).count() == 1


def test_operator_enqueue_is_disabled_without_remote_read(db_session):
    result = enqueue_design_processing_item(
        db_session,
        "123",
        gateway=FailingSnapshotGateway(),
        mode="off",
    )

    assert result["outcome"] == "disabled"
    assert db_session.query(DesignProcessingItem).count() == 0


def test_structured_event_is_canonical_json(caplog):
    event_logger = logging.getLogger("test.design_processing")
    with caplog.at_level(logging.INFO, logger=event_logger.name):
        log_design_processing_event(
            event_logger,
            "test_event",
            item_id="123",
            outcome="queued",
        )

    prefix = "design_processing_event="
    message = caplog.records[-1].getMessage()
    assert message.startswith(prefix)
    payload = json.loads(message[len(prefix) :])
    assert payload["event"] == "test_event"
    assert payload["item_id"] == "123"
    assert payload["outcome"] == "queued"
    assert payload["timestamp"].endswith("+00:00")


def test_operator_failed_job_retry_is_idempotent(db_session):
    identity = _identity()
    item = _item("123", identity, state="failed")
    now = utc_now()
    job = DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=item.item_id,
        trigger_type="test",
        execution_kind="analysis",
        execution_input_revision=identity.input_revision,
        execution_pipeline_version=identity.pipeline_version,
        status="failed",
        stage="extracting",
        scheduled_for=now - timedelta(minutes=5),
        attempt_count=3,
        readiness_check_count=0,
        max_attempts=3,
        completed_at=now,
        last_error="simulated terminal failure",
        created_at=now - timedelta(minutes=5),
        updated_at=now,
    )
    db_session.add_all([item, job])
    db_session.commit()

    first = retry_failed_design_processing_job(
        db_session,
        job.id,
        mode="shadow",
        now=now + timedelta(seconds=1),
    )
    second = retry_failed_design_processing_job(
        db_session,
        job.id,
        mode="shadow",
        now=now + timedelta(seconds=2),
    )

    persisted = db_session.get(DesignProcessingJob, job.id)
    assert first.outcome == "scheduled"
    assert second.outcome == "coalesced"
    assert persisted.status == "scheduled"
    assert persisted.max_attempts == 4
    assert db_session.query(DesignProcessingJob).count() == 1


def test_operator_refuses_disallowed_publication_retry(db_session):
    identity = _identity()
    item = _item("123", identity, state="failed")
    item.latest_analyzed_input_revision = identity.input_revision
    item.latest_analyzed_pipeline_version = identity.pipeline_version
    now = utc_now()
    job = DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=item.item_id,
        trigger_type="test",
        execution_kind="publication",
        execution_input_revision=identity.input_revision,
        execution_pipeline_version=identity.pipeline_version,
        status="failed",
        stage="uploading_ai_data",
        scheduled_for=now,
        attempt_count=3,
        readiness_check_count=0,
        max_attempts=3,
        completed_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add_all([item, job])
    db_session.commit()

    with pytest.raises(ValueError, match="policy does not permit"):
        retry_failed_design_processing_job(
            db_session,
            job.id,
            mode="shadow",
        )

    assert db_session.get(DesignProcessingJob, job.id).status == "failed"


class CleanupGateway:
    def __init__(self):
        self.deleted = []

    def delete_design_file(self, board_id, item_id, column_id, asset_id):
        self.deleted.append((board_id, item_id, column_id, asset_id))


def _seed_cleanup(db_session):
    now = utc_now()
    old_identity = _identity("a" * 64)
    new_identity = _identity("b" * 64)
    item = _item("123", new_identity, state="analyzed")
    item.latest_published_input_revision = new_identity.input_revision
    item.latest_published_pipeline_version = new_identity.pipeline_version
    item.state = "ready_for_review"
    db_session.add(item)
    for kind, column_id, asset_id in (
        ("ai_data", "file_mkza7y37", "1001"),
        ("match_report", "file_mm59rntf", "1002"),
    ):
        content = kind.encode("ascii")
        db_session.add(
            DesignProcessingArtifact(
                id=uuid.uuid4(),
                board_id=BOARD_ID,
                item_id=item.item_id,
                column_id=column_id,
                artifact_kind=kind,
                input_revision=new_identity.input_revision,
                pipeline_version=new_identity.pipeline_version,
                deterministic_filename=f"new-{kind}",
                storage_bucket="bucket",
                storage_object_key=f"new/{kind}",
                content_sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                monday_asset_id=asset_id,
                status="published",
                created_at=now,
                updated_at=now,
            )
        )
        db_session.add(
            DesignProcessingArtifact(
                id=uuid.uuid4(),
                board_id=BOARD_ID,
                item_id=item.item_id,
                column_id=column_id,
                artifact_kind=kind,
                input_revision=old_identity.input_revision,
                pipeline_version=old_identity.pipeline_version,
                deterministic_filename=f"old-{kind}",
                storage_bucket="bucket",
                storage_object_key=f"old/{kind}",
                content_sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                monday_asset_id=str(int(asset_id) - 100),
                status="delete_pending",
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(days=1),
            )
        )
    db_session.commit()


def test_cleanup_retry_obeys_current_mode(monkeypatch, db_session):
    _seed_cleanup(db_session)
    gateway = CleanupGateway()
    monkeypatch.setattr(settings, "design_processing_mode", "shadow")

    blocked = retry_design_processing_cleanup(
        db_session,
        gateway=gateway,
        item_id="123",
    )

    assert blocked == {"itemId": "123", "deleted": 0, "failed": 0}
    assert gateway.deleted == []

    monkeypatch.setattr(settings, "design_processing_mode", "enabled")
    completed = retry_design_processing_cleanup(
        db_session,
        gateway=gateway,
        item_id="123",
    )

    assert completed == {"itemId": "123", "deleted": 2, "failed": 0}
    assert len(gateway.deleted) == 2


def test_metrics_cover_phase7_operational_dimensions(db_session):
    now = utc_now()
    identity = _identity()
    analyzed_item = _item("analyzed", identity, state="analyzed")
    waiting_item = DesignProcessingItem(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id="waiting",
        state="waiting_for_email",
        warnings_json=[],
        created_at=now - timedelta(hours=2),
        updated_at=now,
    )
    waiting_job = DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=waiting_item.item_id,
        trigger_type="test",
        status="retry_wait",
        stage="waiting_for_email",
        scheduled_for=now + timedelta(minutes=1),
        next_retry_at=now + timedelta(minutes=1),
        attempt_count=0,
        readiness_check_count=4,
        max_attempts=3,
        created_at=now - timedelta(hours=2),
        updated_at=now,
    )
    completed_job = DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=analyzed_item.item_id,
        trigger_type="test",
        execution_kind="analysis",
        execution_input_revision=identity.input_revision,
        execution_pipeline_version=identity.pipeline_version,
        status="completed",
        stage="rendering",
        scheduled_for=now - timedelta(minutes=10),
        attempt_count=2,
        readiness_check_count=0,
        max_attempts=3,
        started_at=now - timedelta(minutes=10),
        completed_at=now - timedelta(minutes=5),
        created_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=5),
    )
    event = MondayWebhookEvent(
        id=uuid.uuid4(),
        idempotency_key="phase7-event",
        board_id=BOARD_ID,
        item_id=analyzed_item.item_id,
        payload_json={},
        received_at=now,
        authenticated=True,
        attempt_count=1,
        status="completed",
    )
    dispatch = MondayWebhookDispatch(
        id=uuid.uuid4(),
        webhook_event_id=event.id,
        consumer="design_processing",
        status="succeeded",
        outcome="queued",
        attempt_count=1,
        completed_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add_all(
        [
            analyzed_item,
            waiting_item,
            waiting_job,
            completed_job,
            event,
            dispatch,
        ]
    )
    db_session.commit()

    metrics = collect_design_processing_metrics(db_session, now=now)

    assert metrics["queueDepth"] == {"retry_wait": 1}
    assert metrics["readiness"]["waitingJobs"] == 1
    assert metrics["readiness"]["checksTotal"] == 4
    assert metrics["attempts"]["total"] == 2
    assert metrics["analyzedNotPublished"] == 1
    assert metrics["webhookChildren"]["outcome"] == {"queued": 1}
    assert metrics["artifactCleanup"] == {
        "deletePending": 0,
        "deletePendingWithErrors": 0,
        "deleted": 0,
    }


def test_off_mode_restart_cancels_expired_running_lease(db_session):
    now = utc_now()
    identity = _identity()
    item = _item("expired", identity, state="processing")
    job = DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=item.item_id,
        trigger_type="test",
        execution_kind="analysis",
        execution_input_revision=identity.input_revision,
        execution_pipeline_version=identity.pipeline_version,
        status="running",
        stage="extracting",
        scheduled_for=now - timedelta(hours=2),
        attempt_count=1,
        readiness_check_count=0,
        max_attempts=3,
        locked_at=now - timedelta(hours=2),
        locked_by="dead-worker",
        heartbeat_at=now - timedelta(hours=2),
        started_at=now - timedelta(hours=2),
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=2),
    )
    db_session.add_all([item, job])
    db_session.commit()

    result = run_worker_once(
        db_session,
        mode="off",
        lease_timeout_seconds=60,
    )

    persisted = db_session.get(DesignProcessingJob, job.id)
    assert result.recovered == 1
    assert result.cancelled == 1
    assert result.claimed == 0
    assert persisted.status == "cancelled"
    assert persisted.locked_by is None
