"""add webhook attempt tracking

Revision ID: 0008_webhook_attempt_tracking
Revises: 0007_monday_first_auth
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0008_webhook_attempt_tracking"
down_revision = "0007_monday_first_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "monday_webhook_events",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "monday_webhook_events",
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade():
    op.drop_column("monday_webhook_events", "attempt_count")
    op.drop_column("monday_webhook_events", "processing_started_at")