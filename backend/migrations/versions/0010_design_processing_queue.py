"""add design processing queue

Revision ID: 0010_design_processing_queue
Revises: 0009_snapshot_lifecycle
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_design_processing_queue"
down_revision = "0009_snapshot_lifecycle"
branch_labels = None
depends_on = None


ITEM_STATES = (
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
JOB_STATUSES = (
    "scheduled",
    "running",
    "retry_wait",
    "completed",
    "failed",
    "cancelled",
)
ACTIVE_JOB_STATUSES = ("scheduled", "running", "retry_wait")
JOB_STAGES = (
    "waiting_for_name",
    "waiting_for_email",
    "extracting",
    "matching",
    "rendering",
    "writing_columns",
    "uploading_ai_data",
    "uploading_match_report",
)
EXECUTION_KINDS = ("analysis", "publication")
ARTIFACT_KINDS = ("ai_data", "match_report")
ARTIFACT_STATUSES = (
    "rendered",
    "uploading",
    "published",
    "superseded",
    "delete_pending",
    "deleted",
    "failed",
)
DISPATCH_CONSUMERS = ("auto_sync", "design_processing")
DISPATCH_STATUSES = ("pending", "processing", "succeeded", "failed")
DISPATCH_OUTCOMES = ("queued", "coalesced", "excluded", "ignored", "disabled")


def _sql_values(values):
    return ", ".join(f"'{value}'" for value in values)


def upgrade():
    op.create_table(
        "design_processing_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("latest_desired_input_revision", sa.String(), nullable=True),
        sa.Column("latest_desired_pipeline_version", sa.String(), nullable=True),
        sa.Column("latest_analyzed_input_revision", sa.String(), nullable=True),
        sa.Column("latest_analyzed_pipeline_version", sa.String(), nullable=True),
        sa.Column("latest_published_input_revision", sa.String(), nullable=True),
        sa.Column("latest_published_pipeline_version", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("extracted_parameters_json", sa.JSON(), nullable=True),
        sa.Column("match_result_json", sa.JSON(), nullable=True),
        sa.Column(
            "warnings_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("supersession_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "board_id",
            "item_id",
            name="uq_design_processing_items_board_item",
        ),
        sa.CheckConstraint(
            "((latest_desired_input_revision IS NULL AND "
            "latest_desired_pipeline_version IS NULL) OR "
            "(latest_desired_input_revision IS NOT NULL AND "
            "latest_desired_pipeline_version IS NOT NULL))",
            name="ck_design_processing_items_desired_identity_pair",
        ),
        sa.CheckConstraint(
            "((latest_analyzed_input_revision IS NULL AND "
            "latest_analyzed_pipeline_version IS NULL) OR "
            "(latest_analyzed_input_revision IS NOT NULL AND "
            "latest_analyzed_pipeline_version IS NOT NULL))",
            name="ck_design_processing_items_analyzed_identity_pair",
        ),
        sa.CheckConstraint(
            "((latest_published_input_revision IS NULL AND "
            "latest_published_pipeline_version IS NULL) OR "
            "(latest_published_input_revision IS NOT NULL AND "
            "latest_published_pipeline_version IS NOT NULL))",
            name="ck_design_processing_items_published_identity_pair",
        ),
        sa.CheckConstraint(
            f"state IN ({_sql_values(ITEM_STATES)})",
            name="ck_design_processing_items_state",
        ),
    )
    op.create_index(
        "ix_design_processing_items_state",
        "design_processing_items",
        ["state"],
    )

    op.create_table(
        "design_processing_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("trigger_type", sa.String(), nullable=False),
        sa.Column("execution_kind", sa.String(), nullable=True),
        sa.Column("execution_input_revision", sa.String(), nullable=True),
        sa.Column("execution_pipeline_version", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "readiness_check_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
        ),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_revision", sa.String(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["board_id", "item_id"],
            ["design_processing_items.board_id", "design_processing_items.item_id"],
            name="fk_design_processing_jobs_item",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            f"status IN ({_sql_values(JOB_STATUSES)})",
            name="ck_design_processing_jobs_status",
        ),
        sa.CheckConstraint(
            f"stage IS NULL OR stage IN ({_sql_values(JOB_STAGES)})",
            name="ck_design_processing_jobs_stage",
        ),
        sa.CheckConstraint(
            "((execution_kind IS NULL AND execution_input_revision IS NULL AND "
            "execution_pipeline_version IS NULL) OR "
            "(execution_kind IS NOT NULL AND execution_input_revision IS NOT NULL AND "
            "execution_pipeline_version IS NOT NULL))",
            name="ck_design_processing_jobs_execution_identity",
        ),
        sa.CheckConstraint(
            "execution_kind IS NULL OR "
            f"execution_kind IN ({_sql_values(EXECUTION_KINDS)})",
            name="ck_design_processing_jobs_execution_kind",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND readiness_check_count >= 0 AND max_attempts > 0",
            name="ck_design_processing_jobs_attempt_counts",
        ),
    )
    op.create_index(
        "ix_design_processing_jobs_status_scheduled_for",
        "design_processing_jobs",
        ["status", "scheduled_for"],
    )
    op.create_index(
        "ix_design_processing_jobs_board_item",
        "design_processing_jobs",
        ["board_id", "item_id"],
    )
    op.create_index(
        "ix_design_processing_jobs_status_heartbeat",
        "design_processing_jobs",
        ["status", "heartbeat_at"],
    )
    op.create_index(
        "uq_design_processing_jobs_active_item",
        "design_processing_jobs",
        ["board_id", "item_id"],
        unique=True,
        postgresql_where=sa.text(
            f"status IN ({_sql_values(ACTIVE_JOB_STATUSES)})"
        ),
    )
    op.execute(
        """
        CREATE FUNCTION prevent_design_processing_execution_identity_change()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.execution_kind IS NOT NULL AND (
                NEW.execution_kind IS DISTINCT FROM OLD.execution_kind OR
                NEW.execution_input_revision IS DISTINCT FROM OLD.execution_input_revision OR
                NEW.execution_pipeline_version IS DISTINCT FROM OLD.execution_pipeline_version
            ) THEN
                RAISE EXCEPTION 'design processing execution identity is immutable once assigned';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_design_processing_jobs_execution_identity_immutable
        BEFORE UPDATE ON design_processing_jobs
        FOR EACH ROW
        EXECUTE FUNCTION prevent_design_processing_execution_identity_change()
        """
    )

    op.create_table(
        "design_processing_artifacts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("column_id", sa.String(), nullable=False),
        sa.Column("artifact_kind", sa.String(), nullable=False),
        sa.Column("input_revision", sa.String(), nullable=False),
        sa.Column("pipeline_version", sa.String(), nullable=False),
        sa.Column("deterministic_filename", sa.String(), nullable=False),
        sa.Column("storage_bucket", sa.String(), nullable=False),
        sa.Column("storage_object_key", sa.String(), nullable=False),
        sa.Column("content_sha256", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("monday_asset_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["board_id", "item_id"],
            ["design_processing_items.board_id", "design_processing_items.item_id"],
            name="fk_design_processing_artifacts_item",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "board_id",
            "item_id",
            "column_id",
            "artifact_kind",
            "input_revision",
            "pipeline_version",
            name="uq_design_processing_artifacts_identity",
        ),
        sa.CheckConstraint(
            f"artifact_kind IN ({_sql_values(ARTIFACT_KINDS)})",
            name="ck_design_processing_artifacts_kind",
        ),
        sa.CheckConstraint(
            f"status IN ({_sql_values(ARTIFACT_STATUSES)})",
            name="ck_design_processing_artifacts_status",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="ck_design_processing_artifacts_size",
        ),
    )
    op.create_index(
        "ix_design_processing_artifacts_board_item",
        "design_processing_artifacts",
        ["board_id", "item_id"],
    )
    op.create_index(
        "ix_design_processing_artifacts_status",
        "design_processing_artifacts",
        ["status"],
    )

    op.create_table(
        "monday_webhook_dispatches",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "webhook_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("monday_webhook_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("consumer", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "webhook_event_id",
            "consumer",
            name="uq_monday_webhook_dispatches_event_consumer",
        ),
        sa.CheckConstraint(
            f"consumer IN ({_sql_values(DISPATCH_CONSUMERS)})",
            name="ck_monday_webhook_dispatches_consumer",
        ),
        sa.CheckConstraint(
            f"status IN ({_sql_values(DISPATCH_STATUSES)})",
            name="ck_monday_webhook_dispatches_status",
        ),
        sa.CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_sql_values(DISPATCH_OUTCOMES)})",
            name="ck_monday_webhook_dispatches_outcome",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_monday_webhook_dispatches_attempt_count",
        ),
    )
    op.create_index(
        "ix_monday_webhook_dispatches_status_started",
        "monday_webhook_dispatches",
        ["status", "processing_started_at"],
    )
    op.create_index(
        "ix_monday_webhook_dispatches_webhook_event",
        "monday_webhook_dispatches",
        ["webhook_event_id"],
    )


def downgrade():
    op.drop_index(
        "ix_monday_webhook_dispatches_webhook_event",
        table_name="monday_webhook_dispatches",
    )
    op.drop_index(
        "ix_monday_webhook_dispatches_status_started",
        table_name="monday_webhook_dispatches",
    )
    op.drop_table("monday_webhook_dispatches")

    op.drop_index(
        "ix_design_processing_artifacts_status",
        table_name="design_processing_artifacts",
    )
    op.drop_index(
        "ix_design_processing_artifacts_board_item",
        table_name="design_processing_artifacts",
    )
    op.drop_table("design_processing_artifacts")

    op.execute(
        "DROP TRIGGER trg_design_processing_jobs_execution_identity_immutable "
        "ON design_processing_jobs"
    )
    op.execute("DROP FUNCTION prevent_design_processing_execution_identity_change()")
    op.drop_index(
        "uq_design_processing_jobs_active_item",
        table_name="design_processing_jobs",
    )
    op.drop_index(
        "ix_design_processing_jobs_status_heartbeat",
        table_name="design_processing_jobs",
    )
    op.drop_index(
        "ix_design_processing_jobs_board_item",
        table_name="design_processing_jobs",
    )
    op.drop_index(
        "ix_design_processing_jobs_status_scheduled_for",
        table_name="design_processing_jobs",
    )
    op.drop_table("design_processing_jobs")

    op.drop_index(
        "ix_design_processing_items_state",
        table_name="design_processing_items",
    )
    op.drop_table("design_processing_items")