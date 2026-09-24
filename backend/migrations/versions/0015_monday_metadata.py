"""Current Monday metadata, durable refresh requests, and linked dependencies."""

from alembic import op
import sqlalchemy as sa

revision = "0015_monday_metadata"
down_revision = "0014_reconciliation_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_monday_metadata",
        sa.Column("external_task_key", sa.String(), sa.ForeignKey("tasks.external_task_key", ondelete="CASCADE"), primary_key=True),
        sa.Column("fields_json", sa.JSON(), nullable=True),
        sa.Column("revision", sa.String(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index("ix_task_monday_metadata_scheduled_for", "task_monday_metadata", ["scheduled_for"])
    op.create_table(
        "monday_metadata_links",
        sa.Column("external_task_key", sa.String(), sa.ForeignKey("tasks.external_task_key", ondelete="CASCADE"), primary_key=True),
        sa.Column("column_id", sa.String(), primary_key=True),
        sa.Column("linked_board_id", sa.String(), primary_key=True),
        sa.Column("linked_item_id", sa.String(), primary_key=True),
    )
    op.create_index("ix_monday_metadata_link_source", "monday_metadata_links", ["linked_board_id", "linked_item_id"])


def downgrade() -> None:
    op.drop_table("monday_metadata_links")
    op.drop_table("task_monday_metadata")
