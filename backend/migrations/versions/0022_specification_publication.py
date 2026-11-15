"""Explicit, revision-bound specification publication in active groups."""
from alembic import op
import sqlalchemy as sa

revision = "0022_specification_publication"
down_revision = "0021_processing_acceleration"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("performance_specifications",
                  sa.Column("requested_publication_revision", sa.String(), nullable=True))
    op.add_column("performance_specifications",
                  sa.Column("prepared_revision", sa.String(), nullable=True))


def downgrade():
    op.drop_column("performance_specifications", "prepared_revision")
    op.drop_column("performance_specifications", "requested_publication_revision")
