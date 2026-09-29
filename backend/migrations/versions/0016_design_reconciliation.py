"""Record design reconciliation outcomes and delayed lifecycle rechecks."""

from alembic import op
import sqlalchemy as sa


revision = "0016_design_reconciliation"
down_revision = "0015_monday_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "design_processing_reconciliation_checks",
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.String(), nullable=False),
        sa.Column("last_reason", sa.String(), nullable=False),
        sa.Column("last_group_id", sa.String(), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("board_id", "item_id"),
    )


def downgrade() -> None:
    op.drop_table("design_processing_reconciliation_checks")
