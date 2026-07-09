"""add monday first app users and sessions

Revision ID: 0007_monday_first_auth
Revises: 0006_auto_sync_foundation
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0007_monday_first_auth"
down_revision = "0006_auto_sync_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_users",
        sa.Column("id", sa.String(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("auth_provider", sa.String(), server_default=sa.text("'monday'"), nullable=False),
        sa.Column("monday_account_id", sa.String(), nullable=True),
        sa.Column("monday_user_id", sa.String(), nullable=True),
        sa.Column("monday_email", sa.String(), nullable=True),
        sa.Column("monday_user_name", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("monday_account_id", "monday_user_id", name="uq_app_users_monday_identity"),
    )

    op.add_column("user_monday_links", sa.Column("app_user_id", sa.String(), nullable=True))
    op.add_column("user_monday_links", sa.Column("monday_email", sa.String(), nullable=True))
    op.add_column("user_monday_links", sa.Column("monday_user_name", sa.String(), nullable=True))

    op.execute(
        """
        INSERT INTO app_users (id, auth_provider, created_at, updated_at)
        SELECT DISTINCT target_user_id, 'supabase', now(), now()
        FROM user_monday_links
        WHERE target_user_id IS NOT NULL
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE user_monday_links
        SET app_user_id = target_user_id
        WHERE app_user_id IS NULL
        """
    )
    op.alter_column("user_monday_links", "app_user_id", nullable=False)
    op.create_foreign_key(
        "fk_user_monday_links_app_user_id_app_users",
        "user_monday_links",
        "app_users",
        ["app_user_id"],
        ["id"],
    )
    op.create_index("ix_user_monday_links_app_user_id", "user_monday_links", ["app_user_id"])
    op.create_unique_constraint(
        "uq_user_monday_links_monday_identity",
        "user_monday_links",
        ["monday_account_id", "monday_user_id"],
    )

    op.create_table(
        "app_sessions",
        sa.Column("id", sa.String(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("app_user_id", sa.String(), sa.ForeignKey("app_users.id"), nullable=False),
        sa.Column("session_token_hash", sa.String(), nullable=False),
        sa.Column("csrf_token", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("session_token_hash", name="uq_app_sessions_session_token_hash"),
    )
    op.create_index("ix_app_sessions_app_user_id", "app_sessions", ["app_user_id"])
    op.create_index("ix_app_sessions_expires_at", "app_sessions", ["expires_at"])


def downgrade():
    op.drop_index("ix_app_sessions_expires_at", table_name="app_sessions")
    op.drop_index("ix_app_sessions_app_user_id", table_name="app_sessions")
    op.drop_table("app_sessions")

    op.drop_constraint("uq_user_monday_links_monday_identity", "user_monday_links", type_="unique")
    op.drop_index("ix_user_monday_links_app_user_id", table_name="user_monday_links")
    op.drop_constraint("fk_user_monday_links_app_user_id_app_users", "user_monday_links", type_="foreignkey")
    op.drop_column("user_monday_links", "monday_user_name")
    op.drop_column("user_monday_links", "monday_email")
    op.drop_column("user_monday_links", "app_user_id")
    op.drop_table("app_users")