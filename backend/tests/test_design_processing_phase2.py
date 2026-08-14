from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    DesignProcessingArtifact,
    DesignProcessingItem,
    DesignProcessingJob,
    MondayWebhookDispatch,
    MondayWebhookEvent,
)
from backend.app.services.design_processing_state import (
    AI_DATA_COLUMN_ID,
    MATCH_REPORT_COLUMN_ID,
    InvalidDesignProcessingTransition,
    ProcessingIdentity,
    assign_next_execution,
    cancel_superseded_execution,
    claim_job,
    complete_analysis,
    complete_publication,
    is_ready_for_review,
    needs_analysis,
    needs_publication,
    resume_execution,
    schedule_execution_retry,
    transition_to_readiness_wait,
    update_desired_identity,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _item(
    *,
    desired: ProcessingIdentity | None = None,
    analyzed: ProcessingIdentity | None = None,
    published: ProcessingIdentity | None = None,
    state: str = "scheduled",
    item_id: str = "item-1",
) -> DesignProcessingItem:
    return DesignProcessingItem(
        id=uuid.uuid4(),
        board_id="1882196103",
        item_id=item_id,
        latest_desired_input_revision=(
            desired.input_revision if desired is not None else None
        ),
        latest_desired_pipeline_version=(
            desired.pipeline_version if desired is not None else None
        ),
        latest_analyzed_input_revision=(
            analyzed.input_revision if analyzed is not None else None
        ),
        latest_analyzed_pipeline_version=(
            analyzed.pipeline_version if analyzed is not None else None
        ),
        latest_published_input_revision=(
            published.input_revision if published is not None else None
        ),
        latest_published_pipeline_version=(
            published.pipeline_version if published is not None else None
        ),
        state=state,
        warnings_json=[],
        created_at=NOW,
        updated_at=NOW,
    )


def _job(
    *,
    item_id: str = "item-1",
    status: str = "scheduled",
    execution_kind: str | None = None,
    execution_identity: ProcessingIdentity | None = None,
) -> DesignProcessingJob:
    return DesignProcessingJob(
        id=uuid.uuid4(),
        board_id="1882196103",
        item_id=item_id,
        trigger_type="test",
        execution_kind=execution_kind,
        execution_input_revision=(
            execution_identity.input_revision
            if execution_identity is not None
            else None
        ),
        execution_pipeline_version=(
            execution_identity.pipeline_version
            if execution_identity is not None
            else None
        ),
        status=status,
        scheduled_for=NOW,
        attempt_count=0,
        readiness_check_count=0,
        max_attempts=3,
        created_at=NOW,
        updated_at=NOW,
    )


def _artifact(
    identity: ProcessingIdentity,
    *,
    artifact_kind: str,
    column_id: str,
    status: str = "published",
    monday_asset_id: str | None = "asset-1",
) -> DesignProcessingArtifact:
    return DesignProcessingArtifact(
        id=uuid.uuid4(),
        board_id="1882196103",
        item_id="item-1",
        column_id=column_id,
        artifact_kind=artifact_kind,
        input_revision=identity.input_revision,
        pipeline_version=identity.pipeline_version,
        deterministic_filename=f"{artifact_kind}.bin",
        storage_bucket="private-artifacts",
        storage_object_key=f"design-processing/{artifact_kind}.bin",
        content_sha256="a" * 64,
        size_bytes=10,
        monday_asset_id=monday_asset_id,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


def _published_artifacts(
    identity: ProcessingIdentity,
) -> list[DesignProcessingArtifact]:
    return [
        _artifact(
            identity,
            artifact_kind="ai_data",
            column_id=AI_DATA_COLUMN_ID,
            monday_asset_id="ai-asset",
        ),
        _artifact(
            identity,
            artifact_kind="ai_data_pdf",
            column_id=AI_DATA_COLUMN_ID,
            monday_asset_id="ai-preview-asset",
        ),
        _artifact(
            identity,
            artifact_kind="match_report",
            column_id=MATCH_REPORT_COLUMN_ID,
            monday_asset_id="report-asset",
        ),
    ]


def test_identity_predicates_compare_full_revision_and_pipeline_pairs():
    revision_a_v1 = ProcessingIdentity("revision-a", "pipeline-v1")
    revision_a_v2 = ProcessingIdentity("revision-a", "pipeline-v2")
    item = _item(desired=revision_a_v2, analyzed=revision_a_v1)

    assert needs_analysis(item) is True
    assert needs_publication(item, publication_allowed=True) is False

    item.latest_analyzed_pipeline_version = "pipeline-v2"

    assert needs_analysis(item) is False
    assert needs_publication(item, publication_allowed=False) is False
    assert needs_publication(item, publication_allowed=True) is True


def test_desired_change_immediately_removes_ready_state_and_marks_supersession():
    revision_a = ProcessingIdentity("revision-a", "pipeline-v1")
    revision_b = ProcessingIdentity("revision-b", "pipeline-v1")
    item = _item(
        desired=revision_a,
        analyzed=revision_a,
        published=revision_a,
        state="ready_for_review",
    )
    job = _job(
        status="running",
        execution_kind="analysis",
        execution_identity=revision_a,
    )
    job.locked_by = "worker-1"

    changed = update_desired_identity(item, revision_b, now=NOW, active_job=job)

    assert changed is True
    assert item.state == "processing"
    assert item.supersession_requested_at == NOW
    assert item.latest_published_input_revision == "revision-a"


def test_readiness_wait_uses_name_precedence_without_consuming_normal_attempt():
    identity = ProcessingIdentity("revision-a", "pipeline-v1")
    item = _item(desired=identity)
    job = _job()

    transition_to_readiness_wait(
        item,
        job,
        missing_name=True,
        missing_email=True,
        scheduled_for=NOW + timedelta(seconds=30),
        now=NOW,
    )

    assert item.state == "waiting_for_name"
    assert item.latest_desired_input_revision is None
    assert item.latest_desired_pipeline_version is None
    assert job.status == "retry_wait"
    assert job.stage == "waiting_for_name"
    assert job.readiness_check_count == 1
    assert job.attempt_count == 0
    assert job.next_retry_at == NOW + timedelta(seconds=30)


def test_missing_desired_identity_cannot_be_assigned_as_execution():
    item = _item(state="waiting_for_email")
    job = _job()
    claim_job(job, worker_id="worker-1", now=NOW)

    with pytest.raises(InvalidDesignProcessingTransition):
        assign_next_execution(
            item,
            job,
            worker_id="worker-1",
            publication_allowed=True,
            now=NOW,
        )


def test_analysis_assignment_and_completion_advance_identity_atomically():
    identity = ProcessingIdentity("revision-a", "pipeline-v1")
    item = _item(desired=identity)
    job = _job()
    claim_job(job, worker_id="worker-1", now=NOW)

    obligation = assign_next_execution(
        item,
        job,
        worker_id="worker-1",
        publication_allowed=False,
        now=NOW,
    )

    assert obligation == "analysis"
    assert job.execution_kind == "analysis"
    assert job.execution_input_revision == "revision-a"
    assert job.execution_pipeline_version == "pipeline-v1"
    assert job.attempt_count == 1
    assert item.state == "processing"

    with pytest.raises(InvalidDesignProcessingTransition, match="already assigned"):
        assign_next_execution(
            item,
            job,
            worker_id="worker-1",
            publication_allowed=False,
            now=NOW,
        )

    complete_analysis(item, job, worker_id="worker-1", now=NOW)

    assert item.latest_analyzed_input_revision == "revision-a"
    assert item.latest_analyzed_pipeline_version == "pipeline-v1"
    assert item.state == "analyzed"
    assert job.status == "completed"
    assert job.locked_by is None


def test_redundant_job_preserves_ready_for_review_state():
    identity = ProcessingIdentity("revision-a", "pipeline-v1")
    item = _item(
        desired=identity,
        analyzed=identity,
        published=identity,
        state="ready_for_review",
    )
    job = _job()
    claim_job(job, worker_id="worker-1", now=NOW)

    obligation = assign_next_execution(
        item,
        job,
        worker_id="worker-1",
        publication_allowed=True,
        now=NOW,
    )

    assert obligation is None
    assert job.status == "completed"
    assert item.state == "ready_for_review"


def test_normal_retry_resumes_same_immutable_execution_and_counts_attempt():
    identity = ProcessingIdentity("revision-a", "pipeline-v1")
    item = _item(desired=identity)
    job = _job()
    claim_job(job, worker_id="worker-1", now=NOW)
    assign_next_execution(
        item,
        job,
        worker_id="worker-1",
        publication_allowed=False,
        now=NOW,
    )

    retry_at = NOW + timedelta(minutes=1)
    schedule_execution_retry(
        item,
        job,
        scheduled_for=retry_at,
        error="temporary failure",
        now=NOW,
    )

    assert job.status == "retry_wait"
    assert job.attempt_count == 1
    assert job.locked_by is None

    claim_job(job, worker_id="worker-2", now=retry_at)
    execution_kind = resume_execution(
        item,
        job,
        worker_id="worker-2",
        publication_allowed=False,
        now=retry_at,
    )

    assert execution_kind == "analysis"
    assert job.attempt_count == 2
    assert job.execution_input_revision == "revision-a"


def test_superseded_execution_cannot_complete_and_is_cancelled():
    revision_a = ProcessingIdentity("revision-a", "pipeline-v1")
    revision_b = ProcessingIdentity("revision-b", "pipeline-v1")
    item = _item(desired=revision_a)
    job = _job()
    claim_job(job, worker_id="worker-1", now=NOW)
    assign_next_execution(
        item,
        job,
        worker_id="worker-1",
        publication_allowed=False,
        now=NOW,
    )
    update_desired_identity(item, revision_b, now=NOW, active_job=job)

    with pytest.raises(InvalidDesignProcessingTransition, match="superseded"):
        complete_analysis(item, job, worker_id="worker-1", now=NOW)

    cancel_superseded_execution(item, job, now=NOW)

    assert job.status == "cancelled"
    assert job.superseded_by_revision == "revision-b"
    assert item.state == "scheduled"


def test_publication_completion_requires_all_exact_current_artifacts():
    identity = ProcessingIdentity("revision-a", "pipeline-v1")
    item = _item(desired=identity, analyzed=identity, state="analyzed")
    job = _job()
    claim_job(job, worker_id="worker-1", now=NOW)
    assert assign_next_execution(
        item,
        job,
        worker_id="worker-1",
        publication_allowed=True,
        now=NOW,
    ) == "publication"

    only_ai_data = _published_artifacts(identity)[:1]
    with pytest.raises(InvalidDesignProcessingTransition, match="all current artifacts"):
        complete_publication(
            item,
            job,
            only_ai_data,
            worker_id="worker-1",
            now=NOW,
        )

    artifacts = _published_artifacts(identity)
    complete_publication(
        item,
        job,
        artifacts,
        worker_id="worker-1",
        now=NOW,
    )

    assert item.state == "ready_for_review"
    assert item.latest_published_input_revision == "revision-a"
    assert item.latest_published_pipeline_version == "pipeline-v1"
    assert is_ready_for_review(item, artifacts) is True

    stale_artifacts = _published_artifacts(
        ProcessingIdentity("revision-a", "pipeline-v0")
    )
    assert is_ready_for_review(item, stale_artifacts) is False

    other_item_artifacts = _published_artifacts(identity)
    for artifact in other_item_artifacts:
        artifact.item_id = "other-item"
    assert is_ready_for_review(item, other_item_artifacts) is False


def test_database_rejects_partial_identity_and_invalid_state(db_session):
    partial = _item()
    partial.latest_desired_input_revision = "revision-a"
    partial.latest_desired_pipeline_version = None
    db_session.add(partial)

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    invalid_state = _item(item_id="item-2", state="unknown")
    db_session.add(invalid_state)
    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "unknown"},
        {"stage": "unknown"},
        {"execution_kind": "analysis"},
        {"attempt_count": -1},
        {"max_attempts": 0},
    ],
)
def test_database_rejects_invalid_job_combinations(db_session, changes):
    item = _item()
    job = _job()
    for field_name, value in changes.items():
        setattr(job, field_name, value)
    db_session.add_all([item, job])

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_database_allows_only_one_active_job_per_item(db_session):
    item = _item()
    completed = _job(status="completed")
    active = _job(status="scheduled")
    db_session.add_all([item, completed, active])
    db_session.commit()

    duplicate_active = _job(status="retry_wait")
    db_session.add(duplicate_active)
    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_kind": "unknown"},
        {"status": "unknown"},
        {"size_bytes": -1},
    ],
)
def test_database_rejects_invalid_artifact_values(db_session, changes):
    identity = ProcessingIdentity("revision-a", "pipeline-v1")
    item = _item(desired=identity)
    artifact = _artifact(
        identity,
        artifact_kind="ai_data",
        column_id=AI_DATA_COLUMN_ID,
    )
    for field_name, value in changes.items():
        setattr(artifact, field_name, value)
    db_session.add_all([item, artifact])

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_database_enforces_artifact_identity_uniqueness(db_session):
    identity = ProcessingIdentity("revision-a", "pipeline-v1")
    item = _item(desired=identity)
    first = _artifact(
        identity,
        artifact_kind="ai_data",
        column_id=AI_DATA_COLUMN_ID,
    )
    duplicate = _artifact(
        identity,
        artifact_kind="ai_data",
        column_id=AI_DATA_COLUMN_ID,
    )
    db_session.add_all([item, first, duplicate])

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_database_enforces_one_webhook_child_per_consumer(db_session):
    event = MondayWebhookEvent(
        id=uuid.uuid4(),
        idempotency_key="event-key",
        payload_json={},
        authenticated=True,
        status="processing",
    )
    first = MondayWebhookDispatch(
        id=uuid.uuid4(),
        webhook_event_id=event.id,
        consumer="design_processing",
        status="pending",
        attempt_count=0,
    )
    duplicate = MondayWebhookDispatch(
        id=uuid.uuid4(),
        webhook_event_id=event.id,
        consumer="design_processing",
        status="pending",
        attempt_count=0,
    )
    db_session.add_all([event, first, duplicate])

    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize(
    "changes",
    [
        {"consumer": "unknown"},
        {"status": "unknown"},
        {"outcome": "unknown"},
        {"attempt_count": -1},
    ],
)
def test_database_rejects_invalid_webhook_dispatch_values(db_session, changes):
    event = MondayWebhookEvent(
        id=uuid.uuid4(),
        idempotency_key=str(uuid.uuid4()),
        payload_json={},
        authenticated=True,
        status="processing",
    )
    dispatch = MondayWebhookDispatch(
        id=uuid.uuid4(),
        webhook_event_id=event.id,
        consumer="auto_sync",
        status="pending",
        attempt_count=0,
    )
    for field_name, value in changes.items():
        setattr(dispatch, field_name, value)
    db_session.add_all([event, dispatch])

    with pytest.raises(IntegrityError):
        db_session.commit()