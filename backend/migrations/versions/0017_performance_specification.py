"""Independent CAD specification refresh, dependencies and artifact ledger."""

from alembic import op
import sqlalchemy as sa


revision = "0017_performance_specification"
down_revision = "0016_design_reconciliation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "performance_specifications",
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("requested_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("completed_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("source_json", sa.JSON()),
        sa.Column("current_revision", sa.String()),
        sa.PrimaryKeyConstraint("board_id", "item_id"),
        sa.ForeignKeyConstraint(["board_id", "item_id"],
                                ["design_processing_items.board_id", "design_processing_items.item_id"],
                                ondelete="CASCADE"),
        sa.CheckConstraint("requested_generation >= completed_generation AND completed_generation >= 0"),
    )
    op.create_index("ix_performance_specifications_due", "performance_specifications", ["scheduled_for"])
    op.create_table(
        "performance_specification_links",
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("linked_board_id", sa.String(), nullable=False),
        sa.Column("linked_item_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("board_id", "item_id", "linked_board_id", "linked_item_id"),
        sa.ForeignKeyConstraint(["board_id", "item_id"],
                                ["performance_specifications.board_id", "performance_specifications.item_id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_performance_specification_links_source", "performance_specification_links",
                    ["linked_board_id", "linked_item_id"])
    op.create_table(
        "performance_specification_artifacts",
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("revision", sa.String(), nullable=False),
        sa.Column("translation_json", sa.JSON(), nullable=False),
        sa.Column("model_version", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("storage_bucket", sa.String(), nullable=False),
        sa.Column("storage_object_key", sa.String(), nullable=False),
        sa.Column("content_sha256", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("monday_asset_id", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.PrimaryKeyConstraint("board_id", "item_id", "revision"),
        sa.ForeignKeyConstraint(["board_id", "item_id"],
                                ["performance_specifications.board_id", "performance_specifications.item_id"],
                                ondelete="CASCADE"),
    )


def downgrade():
    op.drop_table("performance_specification_artifacts")
    op.drop_index("ix_performance_specification_links_source", table_name="performance_specification_links")
    op.drop_table("performance_specification_links")
    op.drop_index("ix_performance_specifications_due", table_name="performance_specifications")
    op.drop_table("performance_specifications")
