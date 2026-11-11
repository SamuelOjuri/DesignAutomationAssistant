"""Shared processing-identity availability and probe ownership for Monday items."""

from alembic import op
import sqlalchemy as sa


revision = "0020_monday_item_availability"
down_revision = "0019_specification_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "monday_item_availability",
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("processing_identity", sa.String(64), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("reason", sa.String()),
        sa.Column("actual_board_id", sa.String()),
        sa.Column("actual_group_id", sa.String()),
        sa.Column("actual_item_state", sa.String()),
        sa.Column("consecutive_misses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.Column("last_observed_at", sa.DateTime(timezone=True)),
        sa.Column("last_event_at", sa.DateTime(timezone=True)),
        sa.Column("next_check_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("probe_token", sa.String(64)),
        sa.Column("probe_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("board_id", "item_id", "processing_identity"),
        sa.CheckConstraint("state IN ('unknown', 'available', 'unavailable', 'archived', 'deleted', 'out_of_board', 'retired')",
                           name="ck_monday_item_availability_state"),
        sa.CheckConstraint("consecutive_misses >= 0", name="ck_monday_item_availability_misses"),
        sa.CheckConstraint("(probe_token IS NULL) = (probe_until IS NULL)", name="ck_monday_item_availability_probe"),
    )
    op.create_index("ix_monday_item_availability_due", "monday_item_availability",
                    ["processing_identity", "next_check_at"])


def downgrade():
    op.drop_index("ix_monday_item_availability_due", table_name="monday_item_availability")
    op.drop_table("monday_item_availability")
