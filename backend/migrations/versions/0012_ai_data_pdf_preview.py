"""add AI Data PDF preview artifact

Revision ID: 0012_ai_data_pdf_preview
Revises: 0011_task_file_storage_status
Create Date: 2026-08-14
"""
from alembic import op


revision = "0012_ai_data_pdf_preview"
down_revision = "0011_task_file_storage_status"
branch_labels = None
depends_on = None


OLD_JOB_STAGES = (
    "waiting_for_name",
    "waiting_for_email",
    "extracting",
    "matching",
    "rendering",
    "writing_columns",
    "uploading_ai_data",
    "uploading_match_report",
)
NEW_JOB_STAGES = (
    "waiting_for_name",
    "waiting_for_email",
    "extracting",
    "matching",
    "rendering",
    "writing_columns",
    "uploading_ai_data",
    "uploading_ai_data_pdf",
    "uploading_match_report",
)
OLD_ARTIFACT_KINDS = ("ai_data", "match_report")
NEW_ARTIFACT_KINDS = ("ai_data", "ai_data_pdf", "match_report")


def _sql_values(values):
    return ", ".join(f"'{value}'" for value in values)


def upgrade():
    op.drop_constraint(
        "ck_design_processing_jobs_stage",
        "design_processing_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_design_processing_jobs_stage",
        "design_processing_jobs",
        f"stage IS NULL OR stage IN ({_sql_values(NEW_JOB_STAGES)})",
    )
    op.drop_constraint(
        "ck_design_processing_artifacts_kind",
        "design_processing_artifacts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_design_processing_artifacts_kind",
        "design_processing_artifacts",
        f"artifact_kind IN ({_sql_values(NEW_ARTIFACT_KINDS)})",
    )


def downgrade():
    op.execute(
        "UPDATE design_processing_jobs SET stage = 'uploading_ai_data' "
        "WHERE stage = 'uploading_ai_data_pdf'"
    )
    op.execute(
        "DELETE FROM design_processing_artifacts "
        "WHERE artifact_kind = 'ai_data_pdf'"
    )
    op.drop_constraint(
        "ck_design_processing_jobs_stage",
        "design_processing_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_design_processing_jobs_stage",
        "design_processing_jobs",
        f"stage IS NULL OR stage IN ({_sql_values(OLD_JOB_STAGES)})",
    )
    op.drop_constraint(
        "ck_design_processing_artifacts_kind",
        "design_processing_artifacts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_design_processing_artifacts_kind",
        "design_processing_artifacts",
        f"artifact_kind IN ({_sql_values(OLD_ARTIFACT_KINDS)})",
    )