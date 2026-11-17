"""Shared CRM Accounts directory snapshot for Company Account matching."""
from alembic import op
import sqlalchemy as sa

revision = "0023_crm_directory_snapshots"
down_revision = "0022_specification_publication"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("crm_directory_snapshots",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON()),
        sa.Column("refreshed_at", sa.DateTime(timezone=True)),
        sa.Column("refresh_lease_until", sa.DateTime(timezone=True)))
    if op.get_bind().dialect.name == "postgresql":
        # CRM directory contents are backend-private, including on deployments
        # whose public schema is exposed through Supabase/PostgREST grants.
        op.execute(sa.text('ALTER TABLE "crm_directory_snapshots" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text('REVOKE ALL ON TABLE "crm_directory_snapshots" FROM PUBLIC'))
        for role in ("anon", "authenticated"):
            if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}):
                op.execute(sa.text(f'REVOKE ALL ON TABLE "crm_directory_snapshots" FROM "{role}"'))


def downgrade():
    op.drop_table("crm_directory_snapshots")
