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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from pgvector.sqlalchemy import Vector

from .db import Base


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

    auto_sync_enabled = Column(Boolean, nullable=False, server_default="false")
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
    retention_hold = Column(Boolean, nullable=False, server_default="false")
    retention_hold_at = Column(DateTime(timezone=True), nullable=True)
    retention_hold_by = Column(String, nullable=True)
    retention_hold_reason = Column(Text, nullable=True)
    ingestion_actor = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TaskSnapshot(Base):
    __tablename__ = "task_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    external_task_key = Column(String, ForeignKey("tasks.external_task_key"), nullable=False)

    snapshot_version = Column(String, nullable=False)
    task_context_json = Column(JSON, nullable=False)

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

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    target_user_id = Column(String, nullable=False)  # your app's user id
    monday_user_id = Column(String, nullable=False)
    monday_account_id = Column(String, nullable=False)

    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


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


# Indexes
Index("ix_tasks_auto_sync_state", Task.auto_sync_state)
Index("ix_tasks_purge_after", Task.purge_after)
Index("ix_tasks_source_group_id", Task.source_group_id)
Index("ix_tasks_sync_status", Task.sync_status)
Index("ix_tasks_last_indexed_source_revision", Task.last_indexed_source_revision)
Index("ix_task_snapshots_external_task_key", TaskSnapshot.external_task_key)
Index("ix_task_files_external_task_key", TaskFile.external_task_key)
Index("ix_task_files_snapshot_id", TaskFile.snapshot_id)
Index("ix_task_chunks_file_id", TaskChunk.file_id)
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