"""add task file storage status

Revision ID: 0011_task_file_storage_status
Revises: 0010_design_processing_queue
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa


revision = "0011_task_file_storage_status"
down_revision = "0010_design_processing_queue"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "task_files",
        sa.Column(
            "storage_status",
            sa.String(),
            nullable=False,
            server_default="stored",
        ),
    )
    op.add_column(
        "task_files",
        sa.Column("storage_error_code", sa.String(), nullable=True),
    )
    op.add_column(
        "task_files",
        sa.Column("storage_error_detail", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("task_files", "storage_error_detail")
    op.drop_column("task_files", "storage_error_code")
    op.drop_column("task_files", "storage_status")
