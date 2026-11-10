"""Read-only verification of the applied Specification Review migration."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
url = os.environ["DATABASE_URL"]
engine = create_engine(url, connect_args={"connect_timeout": 10})
tables = (
    "performance_specification_reviews",
    "performance_specification_review_events",
    "performance_specification_approvals",
)
expected_fks = {
    "fk_spec_review_specification", "fk_spec_review_current_approval",
    "fk_spec_review_event_review", "fk_spec_approval_review", "fk_spec_approval_artifact",
}
expected_indexes = {"ix_spec_review_events_item_created", "ix_spec_approvals_item_approved"}
expected_functions = {
    "specification_review_reject_history_change", "specification_review_protect_artifact_payload",
}
try:
    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        schema = connection.scalar(text("SELECT current_schema()"))
        inspector = inspect(connection)
        assert set(tables) <= set(inspector.get_table_names(schema=schema))
        versions = list(connection.scalars(text("SELECT version_num FROM alembic_version")))
        assert versions == ["0019_specification_review"], versions
        fks = {fk["name"] for table in tables for fk in inspector.get_foreign_keys(table, schema=schema)}
        assert fks == expected_fks, sorted(fks)
        indexes = {entry["name"] for table in tables for entry in inspector.get_indexes(table, schema=schema)
                   if not entry.get("duplicates_constraint")}
        assert indexes == expected_indexes, sorted(indexes)
        triggers = connection.execute(text("""
            SELECT c.relname AS table_name, t.tgname AS trigger_name, t.tgenabled AS enabled
            FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema AND NOT t.tgisinternal
              AND t.tgname IN ('reject_history_change', 'protect_artifact_payload')
            ORDER BY c.relname, t.tgname
        """), {"schema": schema}).mappings().all()
        assert {(entry["table_name"], entry["trigger_name"]) for entry in triggers} == {
            ("performance_specification_review_events", "reject_history_change"),
            ("performance_specification_approvals", "reject_history_change"),
            ("performance_specification_artifacts", "protect_artifact_payload"),
        }
        assert all(entry["enabled"] in {"O", "A"} for entry in triggers)
        functions = set(connection.scalars(text("""
            SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = :schema AND p.proname IN
                ('specification_review_reject_history_change', 'specification_review_protect_artifact_payload')
        """), {"schema": schema}))
        assert functions == expected_functions
        access = connection.execute(text("""
            SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled,
                has_table_privilege('anon', c.oid, 'SELECT') AS anon_select,
                has_table_privilege('authenticated', c.oid, 'SELECT') AS authenticated_select
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema AND c.relname IN
                ('performance_specification_reviews', 'performance_specification_review_events',
                 'performance_specification_approvals') ORDER BY c.relname
        """), {"schema": schema}).mappings().all()
        print(json.dumps({
            "configured_target_is_supabase": "supabase" in (make_url(url).host or ""),
            "schema": schema, "revision": versions[0], "tables": list(tables),
            "foreign_keys_verified": sorted(fks), "indexes_verified": sorted(indexes),
            "triggers_verified": [dict(entry) for entry in triggers],
            "functions_verified": sorted(functions), "api_access": [dict(entry) for entry in access],
        }, indent=2))
finally:
    engine.dispose()
