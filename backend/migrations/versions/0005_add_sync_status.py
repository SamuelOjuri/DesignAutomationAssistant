"""add sync status columns to tasks

Revision ID: 0005_add_sync_status
Revises: 0004_task_files_unique
Create Date: 2026-02-02
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0005_add_sync_status"
down_revision = "0004_task_files_unique"
branch_labels = None
depends_on = None


def upgrade():
    # Add sync status tracking columns to tasks table
    op.add_column("tasks", sa.Column("sync_status", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("sync_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("sync_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("sync_error", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("tasks", "sync_error")
    op.drop_column("tasks", "sync_completed_at")
    op.drop_column("tasks", "sync_started_at")
    op.drop_column("tasks", "sync_status")
