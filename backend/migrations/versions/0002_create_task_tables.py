"""create task tables

Revision ID: 0002_create_task_tables
Revises: 0001_enable_pgvector
Create Date: 2026-01-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision = "0002_create_task_tables"
down_revision = "0001_enable_pgvector"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tasks",
        sa.Column("external_task_key", sa.String(), primary_key=True),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_raw_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_snapshot_version", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "task_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("external_task_key", sa.String(), sa.ForeignKey("tasks.external_task_key"), nullable=False),
        sa.Column("snapshot_version", sa.String(), nullable=False),
        sa.Column("task_context_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "task_files",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("external_task_key", sa.String(), sa.ForeignKey("tasks.external_task_key"), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("task_snapshots.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("monday_asset_id", sa.String(), nullable=True),
        sa.Column("original_filename", sa.String(), nullable=True),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("bucket", sa.String(), nullable=False),
        sa.Column("object_path", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "task_chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("task_files.id"), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(), nullable=True),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False), # gemini-embedding-001 (Use 1536)
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "user_monday_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("target_user_id", sa.String(), nullable=False),
        sa.Column("monday_user_id", sa.String(), nullable=False),
        sa.Column("monday_account_id", sa.String(), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "handoff_codes",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("monday_account_id", sa.String(), nullable=False),
        sa.Column("monday_board_id", sa.String(), nullable=False),
        sa.Column("monday_item_id", sa.String(), nullable=False),
        sa.Column("monday_user_id", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # Indexes (matching models)
    op.create_index("ix_task_snapshots_external_task_key", "task_snapshots", ["external_task_key"])
    op.create_index("ix_task_files_external_task_key", "task_files", ["external_task_key"])
    op.create_index("ix_task_files_snapshot_id", "task_files", ["snapshot_id"])
    op.create_index("ix_task_chunks_file_id", "task_chunks", ["file_id"])
    op.create_index("ix_user_monday_links_target_user_id", "user_monday_links", ["target_user_id"])


def downgrade():
    op.drop_index("ix_user_monday_links_target_user_id", table_name="user_monday_links")
    op.drop_index("ix_task_chunks_file_id", table_name="task_chunks")
    op.drop_index("ix_task_files_snapshot_id", table_name="task_files")
    op.drop_index("ix_task_files_external_task_key", table_name="task_files")
    op.drop_index("ix_task_snapshots_external_task_key", table_name="task_snapshots")

    op.drop_table("handoff_codes")
    op.drop_table("user_monday_links")
    op.drop_table("task_chunks")
    op.drop_table("task_files")
    op.drop_table("task_snapshots")
    op.drop_table("tasks")