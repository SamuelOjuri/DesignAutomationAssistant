from alembic import op

revision = "0001_enable_pgvector"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.execute("create extension if not exists vector;")

def downgrade():
    op.execute("drop extension if exists vector;")