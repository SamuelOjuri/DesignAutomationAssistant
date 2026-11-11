"""Private source transcription cache and shared Gemini admission leases."""
from alembic import op
import sqlalchemy as sa

revision = "0021_processing_acceleration"
down_revision = "0020_monday_item_availability"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("source_transcription_cache",
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope_key", "cache_key"))
    op.create_index("ix_source_transcription_cache_expires_at", "source_transcription_cache", ["expires_at"])
    op.create_table("gemini_request_leases",
        sa.Column("token", sa.String(64), primary_key=True),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)))
    op.create_index("ix_gemini_request_leases_scope_started", "gemini_request_leases", ["scope_key", "started_at"])
    if op.get_bind().dialect.name == "postgresql":
        # Transcription text is backend-private, including on deployments whose
        # public schema is exposed through Supabase/PostgREST default grants.
        for table in ("source_transcription_cache", "gemini_request_leases"):
            op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'REVOKE ALL ON TABLE "{table}" FROM PUBLIC'))
            for role in ("anon", "authenticated"):
                if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}):
                    op.execute(sa.text(f'REVOKE ALL ON TABLE "{table}" FROM "{role}"'))


def downgrade():
    op.drop_index("ix_gemini_request_leases_scope_started", table_name="gemini_request_leases")
    op.drop_table("gemini_request_leases")
    op.drop_index("ix_source_transcription_cache_expires_at", table_name="source_transcription_cache")
    op.drop_table("source_transcription_cache")
