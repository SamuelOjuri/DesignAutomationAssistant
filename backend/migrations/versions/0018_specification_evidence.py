"""Retain immutable PDF inputs and review evidence separately from last success."""

from alembic import op
import sqlalchemy as sa

revision = "0018_specification_evidence"
down_revision = "0017_performance_specification"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("performance_specifications", sa.Column("review_json", sa.JSON(), nullable=True))
    op.add_column("performance_specification_artifacts", sa.Column("source_json", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("performance_specification_artifacts", "source_json")
    op.drop_column("performance_specifications", "review_json")
