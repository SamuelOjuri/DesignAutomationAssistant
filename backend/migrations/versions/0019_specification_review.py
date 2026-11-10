"""Durable reviewer decisions, append-only audit and exact-artifact approvals.

Historic artifacts remain untouched: review metadata is written to source_json
only by new review-aware producers and existing PDFs are never auto-approved.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0019_specification_review"
down_revision = "0018_specification_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "performance_specification_reviews",
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("monday_account_id", sa.String(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_fingerprint", sa.String(), nullable=False),
        sa.Column("source_identity_json", sa.JSON(), nullable=False),
        sa.Column("decision_digest", sa.String(64), nullable=False),
        sa.Column("selected_request_type", sa.String()),
        sa.Column("selected_project_board_id", sa.String()),
        sa.Column("selected_project_item_id", sa.String()),
        sa.Column("selected_drawing_board_id", sa.String()),
        sa.Column("selected_drawing_item_id", sa.String()),
        sa.Column("field_decisions_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("source_dispositions_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("desired_publication_identity", sa.String()),
        sa.Column("current_approval_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("board_id", "item_id"),
        sa.ForeignKeyConstraint(
            ["board_id", "item_id"],
            ["performance_specifications.board_id", "performance_specifications.item_id"],
            name="fk_spec_review_specification",
        ),
        sa.UniqueConstraint("monday_account_id", "board_id", "item_id", name="uq_spec_review_tenant_item"),
        sa.CheckConstraint("review_version >= 0 AND schema_version > 0", name="ck_spec_review_versions"),
        sa.CheckConstraint("selected_request_type IS NULL OR selected_request_type IN ('new_enquiry', 'amendment')",
                           name="ck_spec_review_request_type"),
        sa.CheckConstraint("(selected_project_board_id IS NULL) = (selected_project_item_id IS NULL)",
                           name="ck_spec_review_project_pair"),
        sa.CheckConstraint("(selected_drawing_board_id IS NULL) = (selected_drawing_item_id IS NULL)",
                           name="ck_spec_review_drawing_pair"),
        sa.CheckConstraint("selected_drawing_item_id IS NULL OR selected_project_item_id IS NOT NULL",
                           name="ck_spec_review_drawing_project"),
    )
    op.create_table(
        "performance_specification_review_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("monday_account_id", sa.String(), nullable=False),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("actor_app_user_id", sa.String(), nullable=False),
        sa.Column("actor_monday_user_id", sa.String()),
        sa.Column("command_type", sa.String(), nullable=False),
        sa.Column("prior_review_version", sa.Integer(), nullable=False),
        sa.Column("resulting_review_version", sa.Integer(), nullable=False),
        sa.Column("original_decisions_json", sa.JSON(), nullable=False),
        sa.Column("replacement_decisions_json", sa.JSON(), nullable=False),
        sa.Column("source_snapshot_json", sa.JSON()),
        sa.Column("base_source_fingerprint", sa.String(), nullable=False),
        sa.Column("explanation", sa.Text()),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["monday_account_id", "board_id", "item_id"],
            ["performance_specification_reviews.monday_account_id", "performance_specification_reviews.board_id",
             "performance_specification_reviews.item_id"], name="fk_spec_review_event_review",
        ),
        sa.UniqueConstraint("monday_account_id", "board_id", "item_id", "actor_monday_user_id", "idempotency_key",
                            name="uq_spec_review_event_idempotency"),
        sa.CheckConstraint("prior_review_version >= 0 AND resulting_review_version >= prior_review_version",
                           name="ck_spec_review_event_versions"),
    )
    op.create_index("ix_spec_review_events_item_created", "performance_specification_review_events",
                    ["board_id", "item_id", "created_at"])
    op.create_table(
        "performance_specification_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("monday_account_id", sa.String(), nullable=False),
        sa.Column("board_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("artifact_revision", sa.String(), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(), nullable=False),
        sa.Column("extraction_identity", sa.String(), nullable=False),
        sa.Column("decision_digest", sa.String(64), nullable=False),
        sa.Column("selected_project_board_id", sa.String()),
        sa.Column("selected_project_item_id", sa.String()),
        sa.Column("selected_drawing_board_id", sa.String()),
        sa.Column("selected_drawing_item_id", sa.String()),
        sa.Column("actor_app_user_id", sa.String(), nullable=False),
        sa.Column("actor_monday_user_id", sa.String(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["monday_account_id", "board_id", "item_id"],
            ["performance_specification_reviews.monday_account_id", "performance_specification_reviews.board_id",
             "performance_specification_reviews.item_id"], name="fk_spec_approval_review",
        ),
        sa.ForeignKeyConstraint(
            ["board_id", "item_id", "artifact_revision"],
            ["performance_specification_artifacts.board_id", "performance_specification_artifacts.item_id",
             "performance_specification_artifacts.revision"], name="fk_spec_approval_artifact",
        ),
        sa.UniqueConstraint("monday_account_id", "board_id", "item_id", "id", name="uq_spec_approval_tenant_item_id"),
        sa.CheckConstraint("review_version >= 0", name="ck_spec_approval_review_version"),
        sa.CheckConstraint("(selected_project_board_id IS NULL) = (selected_project_item_id IS NULL)",
                           name="ck_spec_approval_project_pair"),
        sa.CheckConstraint("(selected_drawing_board_id IS NULL) = (selected_drawing_item_id IS NULL)",
                           name="ck_spec_approval_drawing_pair"),
        sa.CheckConstraint("selected_drawing_item_id IS NULL OR selected_project_item_id IS NOT NULL",
                           name="ck_spec_approval_drawing_project"),
    )
    op.create_index("ix_spec_approvals_item_approved", "performance_specification_approvals",
                    ["board_id", "item_id", "approved_at"])
    op.create_foreign_key(
        "fk_spec_review_current_approval", "performance_specification_reviews", "performance_specification_approvals",
        ["monday_account_id", "board_id", "item_id", "current_approval_id"],
        ["monday_account_id", "board_id", "item_id", "id"],
    )
    # Protect historic evidence even from an accidental ORM update or parent
    # deletion. The current-approval pointer can be cleared without rewriting it.
    op.execute("""
        CREATE FUNCTION specification_review_reject_history_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Specification review history is immutable: %', TG_TABLE_NAME
                USING ERRCODE = '55000';
        END;
        $$
    """)
    for table in ("performance_specification_review_events", "performance_specification_approvals"):
        op.execute(f"""
            CREATE TRIGGER reject_history_change BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION specification_review_reject_history_change()
        """)
    op.execute("""
        CREATE FUNCTION specification_review_protect_artifact_payload() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.source_json::jsonb IS DISTINCT FROM NEW.source_json::jsonb
               OR OLD.translation_json::jsonb IS DISTINCT FROM NEW.translation_json::jsonb
               OR ROW(OLD.board_id, OLD.item_id, OLD.revision, OLD.model_version,
                      OLD.filename, OLD.storage_bucket, OLD.storage_object_key,
                      OLD.content_sha256, OLD.size_bytes)
                  IS DISTINCT FROM
                  ROW(NEW.board_id, NEW.item_id, NEW.revision, NEW.model_version,
                      NEW.filename, NEW.storage_bucket, NEW.storage_object_key,
                      NEW.content_sha256, NEW.size_bytes) THEN
                RAISE EXCEPTION 'Specification artifact payload is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER protect_artifact_payload BEFORE UPDATE ON performance_specification_artifacts
        FOR EACH ROW EXECUTE FUNCTION specification_review_protect_artifact_payload()
    """)


def downgrade():
    op.execute("DROP TRIGGER protect_artifact_payload ON performance_specification_artifacts")
    op.execute("DROP FUNCTION specification_review_protect_artifact_payload()")
    op.drop_constraint("fk_spec_review_current_approval", "performance_specification_reviews", type_="foreignkey")
    op.drop_table("performance_specification_approvals")
    op.drop_table("performance_specification_review_events")
    op.drop_table("performance_specification_reviews")
    op.execute("DROP FUNCTION specification_review_reject_history_change()")
