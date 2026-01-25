"""add unique constraint for task_files assets

Revision ID: 0004_task_files_unique
Revises: 0003_task_chunks_idx
Create Date: 2026-01-24
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_task_files_unique"
down_revision = "0003_task_chunks_idx"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_task_files_ext_snapshot_asset",
        "task_files",
        ["external_task_key", "snapshot_id", "monday_asset_id"],
    )


def downgrade():
    op.drop_constraint(
        "uq_task_files_ext_snapshot_asset",
        "task_files",
        type_="unique",
    )