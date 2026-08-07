from sqlalchemy import (
    Column,
    String,
    DateTime,
    Integer,
    ForeignKey,
    Text,
    Boolean,
    JSON,
    Index,
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid

from pgvector.sqlalchemy import Vector

from .db import Base


class AppUser(Base):
    __tablename__ = "app_users"
    __table_args__ = (
        UniqueConstraint(
            "monday_account_id",
            "monday_user_id",
            name="uq_app_users_monday_identity",
        ),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    auth_provider = Column(String, nullable=False, server_default="monday", default="monday")
    monday_account_id = Column(String, nullable=True)
    monday_user_id = Column(String, nullable=True)
    monday_email = Column(String, nullable=True)
    monday_user_name = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AppSession(Base):
    __tablename__ = "app_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False)
    session_token_hash = Column(String, nullable=False, unique=True)
    csrf_token = Column(String, nullable=False)

    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    user_agent = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)

    app_user = relationship("AppUser")


class Task(Base):
    __tablename__ = "tasks"

    external_task_key = Column(String, primary_key=True)
    account_id = Column(String, nullable=False)
    board_id = Column(String, nullable=False)
    item_id = Column(String, nullable=False)

    status = Column(String, nullable=True)  # in_progress | done | reopened
    done_at = Column(DateTime(timezone=True), nullable=True)
    delete_raw_after = Column(DateTime(timezone=True), nullable=True)
    raw_purged_at = Column(DateTime(timezone=True), nullable=True)

    latest_snapshot_version = Column(String, nullable=True)
    
    # Sync status tracking for frontend polling
    sync_status = Column(String, nullable=True)  # idle | syncing | completed | failed
    sync_started_at = Column(DateTime(timezone=True), nullable=True)
    sync_completed_at = Column(DateTime(timezone=True), nullable=True)
    sync_error = Column(Text, nullable=True)

    auto_sync_enabled = Column(Boolean, nullable=False, server_default="false", default=False)
    auto_sync_state = Column(String, nullable=True)
    source_group_id = Column(String, nullable=True)
    source_group_title = Column(String, nullable=True)
    auto_synced_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    purge_after = Column(DateTime(timezone=True), nullable=True)
    last_meaningful_access_at = Column(DateTime(timezone=True), nullable=True)
    sync_requested_at = Column(DateTime(timezone=True), nullable=True)
    sync_finished_at = Column(DateTime(timezone=True), nullable=True)
    last_successful_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_trigger = Column(String, nullable=True)
    last_sync_result = Column(String, nullable=True)
    last_indexed_source_revision = Column(String, nullable=True)
    retention_hold = Column(Boolean, nullable=False, server_default="false", default=False)
    retention_hold_at = Column(DateTime(timezone=True), nullable=True)
    retention_hold_by = Column(String, nullable=True)
    retention_hold_reason = Column(Text, nullable=True)
    ingestion_actor = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TaskSnapshot(Base):
    __tablename__ = "task_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "external_task_key",
            "snapshot_version",
            name="uq_task_snapshots_ext_version",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    external_task_key = Column(String, ForeignKey("tasks.external_task_key"), nullable=False)

    snapshot_version = Column(String, nullable=False)
    task_context_json = Column(JSON, nullable=False)
    ingestion_status = Column(String, nullable=False, server_default="complete", default="complete")
    ingestion_error = Column(Text, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    task = relationship("Task")


class TaskFile(Base):
    __tablename__ = "task_files"
    __table_args__ = (
        UniqueConstraint(
            "external_task_key",
            "snapshot_id",
            "monday_asset_id",
            name="uq_task_files_ext_snapshot_asset",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    external_task_key = Column(String, ForeignKey("tasks.external_task_key"), nullable=False)
    snapshot_id = Column(UUID(as_uuid=True), ForeignKey("task_snapshots.id"), nullable=False)

    kind = Column(String, nullable=False)  # email | csv | attachment_pdf | attachment_image | ...
    monday_asset_id = Column(String, nullable=True)
    original_filename = Column(String, nullable=True)
    mime_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)

    bucket = Column(String, nullable=False)
    object_path = Column(String, nullable=False)
    sha256 = Column(String, nullable=True)
    storage_status = Column(String, nullable=False, server_default="stored", default="stored")
    storage_error_code = Column(String, nullable=True)
    storage_error_detail = Column(Text, nullable=True)

    deleted_at = Column(DateTime(timezone=True), nullable=True)
    delete_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    task = relationship("Task")
    snapshot = relationship("TaskSnapshot")


class TaskChunk(Base):
    __tablename__ = "task_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    file_id = Column(UUID(as_uuid=True), ForeignKey("task_files.id"), nullable=False)

    page = Column(Integer, nullable=True)
    section = Column(String, nullable=True)
    chunk_text = Column(Text, nullable=False)

    # change embedding size if your embedding size differs: gemini-embedding-001 (Use 1536)
    embedding = Column(Vector(1536), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    file = relationship("TaskFile")


class UserMondayLink(Base):
    __tablename__ = "user_monday_links"
    __table_args__ = (
        UniqueConstraint(
            "monday_account_id",
            "monday_user_id",
            name="uq_user_monday_links_monday_identity",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), default=uuid.uuid4)
    target_user_id = Column(String, nullable=True)  # legacy Supabase/app user id
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False)
    monday_user_id = Column(String, nullable=False)
    monday_account_id = Column(String, nullable=False)
    monday_email = Column(String, nullable=True)
    monday_user_name = Column(String, nullable=True)

    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    app_user = relationship("AppUser")


class HandoffCode(Base):
    __tablename__ = "handoff_codes"

    code = Column(String, primary_key=True)
    monday_account_id = Column(String, nullable=False)
    monday_board_id = Column(String, nullable=False)
    monday_item_id = Column(String, nullable=False)
    monday_user_id = Column(String, nullable=False)

    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, server_default="false")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MondayWebhookEvent(Base):
    __tablename__ = "monday_webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    idempotency_key = Column(String, nullable=False, unique=True)
    monday_event_id = Column(String, nullable=True)
    subscription_id = Column(String, nullable=True)
    trigger_uuid = Column(String, nullable=True)
    board_id = Column(String, nullable=True)
    item_id = Column(String, nullable=True)
    group_id = Column(String, nullable=True)
    event_type = Column(String, nullable=True)
    column_id = Column(String, nullable=True)
    payload_json = Column(JSON, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    authenticated = Column(Boolean, nullable=False, server_default="false")
    processed_at = Column(DateTime(timezone=True), nullable=True)
    processing_started_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, server_default="0", default=0)
    status = Column(String, nullable=False)
    error = Column(Text, nullable=True)


class AutoSyncJob(Base):
    __tablename__ = "auto_sync_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    board_id = Column(String, nullable=False)
    item_id = Column(String, nullable=False)
    external_task_key = Column(String, ForeignKey("tasks.external_task_key"), nullable=True)
    trigger_type = Column(String, nullable=False)
    desired_source_revision = Column(String, nullable=True)
    status = Column(String, nullable=False)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default="3")
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    locked_by = Column(String, nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    task = relationship("Task")


DESIGN_PROCESSING_ITEM_STATES = (
    "waiting_for_name",
    "waiting_for_email",
    "scheduled",
    "processing",
    "analyzed",
    "publishing",
    "ready_for_review",
    "ineligible",
    "failed",
)
DESIGN_PROCESSING_JOB_STATUSES = (
    "scheduled",
    "running",
    "retry_wait",
    "completed",
    "failed",
    "cancelled",
)
DESIGN_PROCESSING_ACTIVE_JOB_STATUSES = (
    "scheduled",
    "running",
    "retry_wait",
)
DESIGN_PROCESSING_JOB_STAGES = (
    "waiting_for_name",
    "waiting_for_email",
    "extracting",
    "matching",
    "rendering",
    "writing_columns",
    "uploading_ai_data",
    "uploading_match_report",
)
DESIGN_PROCESSING_EXECUTION_KINDS = ("analysis", "publication")
DESIGN_PROCESSING_ARTIFACT_KINDS = ("ai_data", "match_report")
DESIGN_PROCESSING_ARTIFACT_STATUSES = (
    "rendered",
    "uploading",
    "published",
    "superseded",
    "delete_pending",
    "deleted",
    "failed",
)
MONDAY_WEBHOOK_DISPATCH_CONSUMERS = ("auto_sync", "design_processing")
MONDAY_WEBHOOK_DISPATCH_STATUSES = (
    "pending",
    "processing",
    "succeeded",
    "failed",
)
MONDAY_WEBHOOK_DISPATCH_OUTCOMES = (
    "queued",
    "coalesced",
    "excluded",
    "ignored",
    "disabled",
)


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class DesignProcessingItem(Base):
    __tablename__ = "design_processing_items"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "item_id",
            name="uq_design_processing_items_board_item",
        ),
        CheckConstraint(
            "((latest_desired_input_revision IS NULL AND "
            "latest_desired_pipeline_version IS NULL) OR "
            "(latest_desired_input_revision IS NOT NULL AND "
            "latest_desired_pipeline_version IS NOT NULL))",
            name="ck_design_processing_items_desired_identity_pair",
        ),
        CheckConstraint(
            "((latest_analyzed_input_revision IS NULL AND "
            "latest_analyzed_pipeline_version IS NULL) OR "
            "(latest_analyzed_input_revision IS NOT NULL AND "
            "latest_analyzed_pipeline_version IS NOT NULL))",
            name="ck_design_processing_items_analyzed_identity_pair",
        ),
        CheckConstraint(
            "((latest_published_input_revision IS NULL AND "
            "latest_published_pipeline_version IS NULL) OR "
            "(latest_published_input_revision IS NOT NULL AND "
            "latest_published_pipeline_version IS NOT NULL))",
            name="ck_design_processing_items_published_identity_pair",
        ),
        CheckConstraint(
            f"state IN ({_sql_values(DESIGN_PROCESSING_ITEM_STATES)})",
            name="ck_design_processing_items_state",
        ),
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        default=uuid.uuid4,
    )
    board_id = Column(String, nullable=False)
    item_id = Column(String, nullable=False)
    latest_desired_input_revision = Column(String, nullable=True)
    latest_desired_pipeline_version = Column(String, nullable=True)
    latest_analyzed_input_revision = Column(String, nullable=True)
    latest_analyzed_pipeline_version = Column(String, nullable=True)
    latest_published_input_revision = Column(String, nullable=True)
    latest_published_pipeline_version = Column(String, nullable=True)
    state = Column(String, nullable=False)
    extracted_parameters_json = Column(JSON, nullable=True)
    match_result_json = Column(JSON, nullable=True)
    warnings_json = Column(JSON, nullable=False, default=list, server_default="[]")
    supersession_requested_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class DesignProcessingJob(Base):
    __tablename__ = "design_processing_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["board_id", "item_id"],
            ["design_processing_items.board_id", "design_processing_items.item_id"],
            name="fk_design_processing_jobs_item",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(DESIGN_PROCESSING_JOB_STATUSES)})",
            name="ck_design_processing_jobs_status",
        ),
        CheckConstraint(
            "stage IS NULL OR "
            f"stage IN ({_sql_values(DESIGN_PROCESSING_JOB_STAGES)})",
            name="ck_design_processing_jobs_stage",
        ),
        CheckConstraint(
            "((execution_kind IS NULL AND execution_input_revision IS NULL AND "
            "execution_pipeline_version IS NULL) OR "
            "(execution_kind IS NOT NULL AND execution_input_revision IS NOT NULL AND "
            "execution_pipeline_version IS NOT NULL))",
            name="ck_design_processing_jobs_execution_identity",
        ),
        CheckConstraint(
            "execution_kind IS NULL OR "
            f"execution_kind IN ({_sql_values(DESIGN_PROCESSING_EXECUTION_KINDS)})",
            name="ck_design_processing_jobs_execution_kind",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND readiness_check_count >= 0 AND max_attempts > 0",
            name="ck_design_processing_jobs_attempt_counts",
        ),
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        default=uuid.uuid4,
    )
    board_id = Column(String, nullable=False)
    item_id = Column(String, nullable=False)
    trigger_type = Column(String, nullable=False)
    execution_kind = Column(String, nullable=True)
    execution_input_revision = Column(String, nullable=True)
    execution_pipeline_version = Column(String, nullable=True)
    status = Column(String, nullable=False)
    stage = Column(String, nullable=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    readiness_check_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_attempts = Column(Integer, nullable=False, default=3, server_default="3")
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    locked_by = Column(String, nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    superseded_by_revision = Column(String, nullable=True)
    last_error = Column(Text, nullable=True)
    result_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    item = relationship("DesignProcessingItem")


class DesignProcessingArtifact(Base):
    __tablename__ = "design_processing_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["board_id", "item_id"],
            ["design_processing_items.board_id", "design_processing_items.item_id"],
            name="fk_design_processing_artifacts_item",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "board_id",
            "item_id",
            "column_id",
            "artifact_kind",
            "input_revision",
            "pipeline_version",
            name="uq_design_processing_artifacts_identity",
        ),
        CheckConstraint(
            f"artifact_kind IN ({_sql_values(DESIGN_PROCESSING_ARTIFACT_KINDS)})",
            name="ck_design_processing_artifacts_kind",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(DESIGN_PROCESSING_ARTIFACT_STATUSES)})",
            name="ck_design_processing_artifacts_status",
        ),
        CheckConstraint(
            "size_bytes >= 0",
            name="ck_design_processing_artifacts_size",
        ),
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        default=uuid.uuid4,
    )
    board_id = Column(String, nullable=False)
    item_id = Column(String, nullable=False)
    column_id = Column(String, nullable=False)
    artifact_kind = Column(String, nullable=False)
    input_revision = Column(String, nullable=False)
    pipeline_version = Column(String, nullable=False)
    deterministic_filename = Column(String, nullable=False)
    storage_bucket = Column(String, nullable=False)
    storage_object_key = Column(String, nullable=False)
    content_sha256 = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    monday_asset_id = Column(String, nullable=True)
    status = Column(String, nullable=False)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    item = relationship("DesignProcessingItem")


class MondayWebhookDispatch(Base):
    __tablename__ = "monday_webhook_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "webhook_event_id",
            "consumer",
            name="uq_monday_webhook_dispatches_event_consumer",
        ),
        CheckConstraint(
            f"consumer IN ({_sql_values(MONDAY_WEBHOOK_DISPATCH_CONSUMERS)})",
            name="ck_monday_webhook_dispatches_consumer",
        ),
        CheckConstraint(
            f"status IN ({_sql_values(MONDAY_WEBHOOK_DISPATCH_STATUSES)})",
            name="ck_monday_webhook_dispatches_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR "
            f"outcome IN ({_sql_values(MONDAY_WEBHOOK_DISPATCH_OUTCOMES)})",
            name="ck_monday_webhook_dispatches_outcome",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_monday_webhook_dispatches_attempt_count",
        ),
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        default=uuid.uuid4,
    )
    webhook_event_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "monday_webhook_events.id",
            name="monday_webhook_dispatches_webhook_event_id_fkey",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    consumer = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending", server_default="pending")
    outcome = Column(String, nullable=True)
    job_id = Column(UUID(as_uuid=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    processing_started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    result_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    webhook_event = relationship("MondayWebhookEvent")


# Indexes
Index("ix_app_sessions_app_user_id", AppSession.app_user_id)
Index("ix_app_sessions_expires_at", AppSession.expires_at)
Index("ix_tasks_auto_sync_state", Task.auto_sync_state)
Index("ix_tasks_purge_after", Task.purge_after)
Index("ix_tasks_source_group_id", Task.source_group_id)
Index("ix_tasks_sync_status", Task.sync_status)
Index("ix_tasks_last_indexed_source_revision", Task.last_indexed_source_revision)
Index("ix_task_snapshots_external_task_key", TaskSnapshot.external_task_key)
Index("ix_task_files_external_task_key", TaskFile.external_task_key)
Index("ix_task_files_snapshot_id", TaskFile.snapshot_id)
Index("ix_task_chunks_file_id", TaskChunk.file_id)
Index("ix_user_monday_links_app_user_id", UserMondayLink.app_user_id)
Index("ix_user_monday_links_target_user_id", UserMondayLink.target_user_id)
Index("ix_monday_webhook_events_board_item", MondayWebhookEvent.board_id, MondayWebhookEvent.item_id)
Index("ix_monday_webhook_events_received_at", MondayWebhookEvent.received_at)
Index("ix_monday_webhook_events_status", MondayWebhookEvent.status)
Index("ix_auto_sync_jobs_status_scheduled_for", AutoSyncJob.status, AutoSyncJob.scheduled_for)
Index("ix_auto_sync_jobs_board_item", AutoSyncJob.board_id, AutoSyncJob.item_id)
Index(
    "uq_auto_sync_jobs_active_item",
    AutoSyncJob.board_id,
    AutoSyncJob.item_id,
    unique=True,
    postgresql_where=AutoSyncJob.status.in_(("pending", "scheduled", "running", "retry_wait")),
)
Index("ix_design_processing_items_state", DesignProcessingItem.state)
Index(
    "ix_design_processing_jobs_status_scheduled_for",
    DesignProcessingJob.status,
    DesignProcessingJob.scheduled_for,
)
Index(
    "ix_design_processing_jobs_board_item",
    DesignProcessingJob.board_id,
    DesignProcessingJob.item_id,
)
Index(
    "ix_design_processing_jobs_status_heartbeat",
    DesignProcessingJob.status,
    DesignProcessingJob.heartbeat_at,
)
Index(
    "uq_design_processing_jobs_active_item",
    DesignProcessingJob.board_id,
    DesignProcessingJob.item_id,
    unique=True,
    postgresql_where=DesignProcessingJob.status.in_(DESIGN_PROCESSING_ACTIVE_JOB_STATUSES),
    sqlite_where=DesignProcessingJob.status.in_(DESIGN_PROCESSING_ACTIVE_JOB_STATUSES),
)
Index(
    "ix_design_processing_artifacts_board_item",
    DesignProcessingArtifact.board_id,
    DesignProcessingArtifact.item_id,
)
Index(
    "ix_design_processing_artifacts_status",
    DesignProcessingArtifact.status,
)
Index(
    "ix_monday_webhook_dispatches_status_started",
    MondayWebhookDispatch.status,
    MondayWebhookDispatch.processing_started_at,
)
Index(
    "ix_monday_webhook_dispatches_webhook_event",
    MondayWebhookDispatch.webhook_event_id,
)