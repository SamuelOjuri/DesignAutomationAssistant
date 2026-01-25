"""shrink embedding to 1536 and add hnsw index

Revision ID: 0003_add_task_chunks_embedding_index
Revises: 0002_create_task_tables
Create Date: 2026-01-23
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_task_chunks_idx"
down_revision = "0002_create_task_tables"
branch_labels = None
depends_on = None


def upgrade():
    # No data yet: drop and recreate with 1536 dims
    op.execute("ALTER TABLE task_chunks DROP COLUMN embedding;")
    op.execute("ALTER TABLE task_chunks ADD COLUMN embedding vector(1536) NOT NULL;")

    # HNSW index for cosine similarity (valid for <= 2000 dims)
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_task_chunks_embedding_hnsw
        ON task_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);
        """
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_task_chunks_embedding_hnsw;")

