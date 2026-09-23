"""Persist fair reconciliation progress and successful check times."""

from alembic import op
import sqlalchemy as sa


revision = "0014_reconciliation_checks"
down_revision = "0013_sync_generations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auto_sync_reconciliation_checks",
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.String(), nullable=False),
        sa.Column("last_reason", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("board_id", "item_id", "scope"),
    )


def downgrade() -> None:
    op.drop_table("auto_sync_reconciliation_checks")