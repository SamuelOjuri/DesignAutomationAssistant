"""Persist auto-sync request generations and forced refresh intent."""

from alembic import op
import sqlalchemy as sa


revision = "0013_sync_generations"
down_revision = "0012_ai_data_pdf_preview"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("auto_sync_jobs", sa.Column("desired_generation", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("auto_sync_jobs", sa.Column("execution_generation", sa.Integer(), nullable=True))
    op.add_column("auto_sync_jobs", sa.Column("execution_source_revision", sa.String(), nullable=True))
    op.add_column("auto_sync_jobs", sa.Column("execution_trigger_type", sa.String(), nullable=True))
    op.add_column("auto_sync_jobs", sa.Column("force_requested", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("auto_sync_jobs", sa.Column("execution_force", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    for name in ("execution_force", "force_requested", "execution_trigger_type",
                 "execution_source_revision", "execution_generation", "desired_generation"):
        op.drop_column("auto_sync_jobs", name)