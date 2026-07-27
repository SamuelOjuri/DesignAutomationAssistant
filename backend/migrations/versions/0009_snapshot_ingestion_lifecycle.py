"""add snapshot ingestion lifecycle

Revision ID: 0009_snapshot_lifecycle
Revises: 0008_webhook_attempt_tracking
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa


revision = "0009_snapshot_lifecycle"
down_revision = "0008_webhook_attempt_tracking"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "task_snapshots",
        sa.Column(
            "ingestion_status",
            sa.String(),
            server_default=sa.text("'complete'"),
            nullable=False,
        ),
    )
    op.add_column(
        "task_snapshots",
        sa.Column("ingestion_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "task_snapshots",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE task_snapshots SET completed_at = created_at "
        "WHERE ingestion_status = 'complete' AND completed_at IS NULL"
    )
    op.execute(
        """
        CREATE TEMP TABLE snapshot_dedup_map ON COMMIT DROP AS
        SELECT id AS duplicate_id, keeper_id
        FROM (
            SELECT
                id,
                first_value(id) OVER (
                    PARTITION BY external_task_key, snapshot_version
                    ORDER BY created_at DESC, id DESC
                ) AS keeper_id,
                row_number() OVER (
                    PARTITION BY external_task_key, snapshot_version
                    ORDER BY created_at DESC, id DESC
                ) AS duplicate_number
            FROM task_snapshots
        ) ranked
        WHERE duplicate_number > 1
        """
    )
    op.execute(
        """
        DELETE FROM task_chunks
        WHERE file_id IN (
            SELECT duplicate_file.id
            FROM task_files duplicate_file
            JOIN snapshot_dedup_map map
              ON map.duplicate_id = duplicate_file.snapshot_id
            WHERE EXISTS (
                SELECT 1
                FROM task_files keeper_file
                WHERE keeper_file.snapshot_id = map.keeper_id
                  AND keeper_file.external_task_key = duplicate_file.external_task_key
                  AND keeper_file.monday_asset_id = duplicate_file.monday_asset_id
            )
        )
        """
    )
    op.execute(
        """
        DELETE FROM task_files duplicate_file
        USING snapshot_dedup_map map
        WHERE duplicate_file.snapshot_id = map.duplicate_id
          AND EXISTS (
              SELECT 1
              FROM task_files keeper_file
              WHERE keeper_file.snapshot_id = map.keeper_id
                AND keeper_file.external_task_key = duplicate_file.external_task_key
                AND keeper_file.monday_asset_id = duplicate_file.monday_asset_id
          )
        """
    )
    op.execute(
        """
        UPDATE task_files file_record
        SET snapshot_id = map.keeper_id
        FROM snapshot_dedup_map map
        WHERE file_record.snapshot_id = map.duplicate_id
        """
    )
    op.execute(
        """
        DELETE FROM task_snapshots snapshot
        USING snapshot_dedup_map map
        WHERE snapshot.id = map.duplicate_id
        """
    )
    op.create_unique_constraint(
        "uq_task_snapshots_ext_version",
        "task_snapshots",
        ["external_task_key", "snapshot_version"],
    )


def downgrade():
    op.drop_constraint(
        "uq_task_snapshots_ext_version",
        "task_snapshots",
        type_="unique",
    )
    op.drop_column("task_snapshots", "completed_at")
    op.drop_column("task_snapshots", "ingestion_error")
    op.drop_column("task_snapshots", "ingestion_status")