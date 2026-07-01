"""add auto sync foundation

Revision ID: 0006_auto_sync_foundation
Revises: 0005_add_sync_status
Create Date: 2026-07-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "0006_auto_sync_foundation"
down_revision = "0005_add_sync_status"
branch_labels = None
depends_on = None


ACTIVE_JOB_STATUSES = "('pending', 'scheduled', 'running', 'retry_wait')"


def upgrade():
    op.add_column("tasks", sa.Column("auto_sync_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("tasks", sa.Column("auto_sync_state", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("source_group_id", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("source_group_title", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("auto_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("last_meaningful_access_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("sync_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("sync_finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("last_successful_sync_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("last_sync_trigger", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("last_sync_result", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("last_indexed_source_revision", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("retention_hold", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("tasks", sa.Column("retention_hold_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("retention_hold_by", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("retention_hold_reason", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("ingestion_actor", sa.String(), nullable=True))

    op.create_index("ix_tasks_auto_sync_state", "tasks", ["auto_sync_state"])
    op.create_index("ix_tasks_purge_after", "tasks", ["purge_after"])
    op.create_index("ix_tasks_source_group_id", "tasks", ["source_group_id"])
    op.create_index("ix_tasks_sync_status", "tasks", ["sync_status"])
    op.create_index("ix_tasks_last_indexed_source_revision", "tasks", ["last_indexed_source_revision"])

    op.create_table(
        "monday_webhook_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("monday_event_id", sa.String(), nullable=True),
        sa.Column("subscription_id", sa.String(), nullable=True),
        sa.Column("trigger_uuid", sa.String(), nullable=True),
        sa.Column("board_id", sa.String(), nullable=True),
        sa.Column("item_id", sa.String(), nullable=True),
        sa.Column("group_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=True),
        sa.Column("column_id", sa.String(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("authenticated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_monday_webhook_events_idempotency_key"),
    )
    op.create_index("ix_monday_webhook_events_board_item", "monday_webhook_events", ["board_id", "item_id"])
    op.create_index("ix_monday_webhook_events_received_at", "monday_webhook_events", ["received_at"])
    op.create_index("ix_monday_webhook_events_status", "monday_webhook_events", ["status"])

    op.create_table(
        "auto_sync_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("external_task_key", sa.String(), sa.ForeignKey("tasks.external_task_key"), nullable=True),
        sa.Column("trigger_type", sa.String(), nullable=False),
        sa.Column("desired_source_revision", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_auto_sync_jobs_status_scheduled_for", "auto_sync_jobs", ["status", "scheduled_for"])
    op.create_index("ix_auto_sync_jobs_board_item", "auto_sync_jobs", ["board_id", "item_id"])
    op.create_index(
        "uq_auto_sync_jobs_active_item",
        "auto_sync_jobs",
        ["board_id", "item_id"],
        unique=True,
        postgresql_where=sa.text(f"status in {ACTIVE_JOB_STATUSES}"),
    )


def downgrade():
    op.drop_index("uq_auto_sync_jobs_active_item", table_name="auto_sync_jobs")
    op.drop_index("ix_auto_sync_jobs_board_item", table_name="auto_sync_jobs")
    op.drop_index("ix_auto_sync_jobs_status_scheduled_for", table_name="auto_sync_jobs")
    op.drop_table("auto_sync_jobs")

    op.drop_index("ix_monday_webhook_events_status", table_name="monday_webhook_events")
    op.drop_index("ix_monday_webhook_events_received_at", table_name="monday_webhook_events")
    op.drop_index("ix_monday_webhook_events_board_item", table_name="monday_webhook_events")
    op.drop_table("monday_webhook_events")

    op.drop_index("ix_tasks_last_indexed_source_revision", table_name="tasks")
    op.drop_index("ix_tasks_sync_status", table_name="tasks")
    op.drop_index("ix_tasks_source_group_id", table_name="tasks")
    op.drop_index("ix_tasks_purge_after", table_name="tasks")
    op.drop_index("ix_tasks_auto_sync_state", table_name="tasks")

    op.drop_column("tasks", "ingestion_actor")
    op.drop_column("tasks", "retention_hold_reason")
    op.drop_column("tasks", "retention_hold_by")
    op.drop_column("tasks", "retention_hold_at")
    op.drop_column("tasks", "retention_hold")
    op.drop_column("tasks", "last_indexed_source_revision")
    op.drop_column("tasks", "last_sync_result")
    op.drop_column("tasks", "last_sync_trigger")
    op.drop_column("tasks", "last_successful_sync_at")
    op.drop_column("tasks", "sync_finished_at")
    op.drop_column("tasks", "sync_requested_at")
    op.drop_column("tasks", "last_meaningful_access_at")
    op.drop_column("tasks", "purge_after")
    op.drop_column("tasks", "completed_at")
    op.drop_column("tasks", "auto_synced_at")
    op.drop_column("tasks", "source_group_title")
    op.drop_column("tasks", "source_group_id")
    op.drop_column("tasks", "auto_sync_state")
    op.drop_column("tasks", "auto_sync_enabled")