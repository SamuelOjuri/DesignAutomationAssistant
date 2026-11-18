"""Optional real PostgreSQL lifecycle, queue fencing and diagnostic SQL checks.

Set TEST_DATABASE_URL to a disposable PostgreSQL database with pgvector. The
shared fixture migrates and drops only a UUID-named isolated schema. No Monday,
model, storage or configured application database connection is used.
"""

from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text

from backend.app.config import settings
from backend.app.models import (
    DesignProcessingItem, DesignProcessingReconciliationCheck, PerformanceSpecification,
    PerformanceSpecificationArtifact, Task,
)
from backend.app.services import performance_specification as worker
from backend.app.services.monday_item_availability import (
    observe_item, observe_unavailable, processing_identity,
)
from backend.app.services.performance_specification_contract import SPECIFICATION_VERSION
from backend.app.services.specification_policy import SPECIFICATION_SCOPE_POLICY_VERSION
from backend.app.services.specification_references import REFERENCE_PARSER_VERSION
from backend.app.services.specification_review_contract import SOURCE_POLICY_VERSION
from backend.tests.test_specification_review_postgres_concurrency import postgres_review_database, race


BOARD = "1882196103"
DIAGNOSTIC_SQL = (Path(__file__).resolve().parents[1] / "scripts" /
    "diagnose_performance_specifications.sql").read_text(encoding="utf-8")


def _live_query(observed_at, items, *, board_id=BOARD, query=DIAGNOSTIC_SQL):
    snapshot = json.dumps({
        "board_id": board_id,
        "observed_at": observed_at.isoformat(),
        "items": [{"item_id": item_id, "group_id": group_id, "group_title": title}
                  for item_id, group_id, title in items],
    })
    return query.replace("$monday_snapshot${}$monday_snapshot$", f"$monday_snapshot${snapshot}$monday_snapshot$")


def _diagnostic_rows(db, query=DIAGNOSTIC_SQL):
    db.execute(text("SET TRANSACTION READ ONLY"))
    rows = db.execute(text(query)).mappings().all()
    db.rollback()
    return {row["item_id"]: row for row in rows}


@pytest.fixture()
def postgres_specification(postgres_review_database, monkeypatch):
    engine, Session = postgres_review_database
    item_id = str(uuid.uuid4().int)
    monkeypatch.setattr(settings, "performance_specification_enabled", True)
    monkeypatch.setattr(settings, "design_processing_board_id", BOARD)
    monkeypatch.setattr(settings, "design_processing_landing_group_id", "group_mkpbd6vy")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "group_mkpbb3tx")
    monkeypatch.setattr(settings, "performance_specification_mode", "allowlist")
    monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", [item_id])
    with Session() as db:
        db.add(DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id=item_id,
            state="ready_for_review"))
        db.commit()
    return engine, Session, item_id


def _enqueue(db, item_id):
    record = worker.enqueue_specification(db, BOARD, item_id,
        group_id=settings.design_processing_landing_group_id, immediate=True, reconcile=True)
    db.commit()
    return record.requested_generation


def test_competing_reconciliation_creates_one_queue_record_and_generation(postgres_specification):
    _, Session, item_id = postgres_specification
    results = race(Session, [lambda db: _enqueue(db, item_id), lambda db: _enqueue(db, item_id)])
    assert results == [1, 1], results
    with Session() as db:
        record = db.get(PerformanceSpecification, (BOARD, item_id))
        assert record.requested_generation == 1 and record.completed_generation == 0
        assert db.query(PerformanceSpecification).filter_by(board_id=BOARD, item_id=item_id).count() == 1
        assert db.query(Task).filter_by(board_id=BOARD, item_id=item_id).count() == 0


def test_competing_workers_receive_only_one_lease(postgres_specification):
    _, Session, item_id = postgres_specification
    with Session() as db:
        _enqueue(db, item_id)
    results = race(Session, [worker.claim_specification, worker.claim_specification])
    claims = [result for result in results if isinstance(result, tuple)]
    assert len(claims) == 1 and results.count(None) == 1, results
    with Session() as db:
        record = db.get(PerformanceSpecification, (BOARD, item_id))
        assert record.lease_token == claims[0][2]
        assert record.requested_generation == claims[0][3] == 1


def test_cancel_revokes_claim_and_reentry_retains_published_artifact(postgres_specification):
    _, Session, item_id = postgres_specification
    with Session() as db:
        _enqueue(db, item_id)
        record = db.get(PerformanceSpecification, (BOARD, item_id))
        record.current_revision = "retained-pdf"
        db.add(PerformanceSpecificationArtifact(board_id=BOARD, item_id=item_id, revision="retained-pdf",
            translation_json={}, source_json={}, model_version="test", filename="retained.pdf",
            storage_bucket="private", storage_object_key="retained.pdf", content_sha256="a" * 64,
            size_bytes=1, monday_asset_id="retained-asset", status="published"))
        db.commit()
        claim = worker.claim_specification(db)
    with Session() as db:
        worker.cancel_specification(db, BOARD, item_id, reason="outside_landing_zone")
        db.commit()
    with Session() as db:
        with pytest.raises(worker.SupersededSpecification):
            worker._check_claim(db, claim)
        record = db.get(PerformanceSpecification, (BOARD, item_id))
        assert record.lease_token is record.scheduled_for is None
        assert record.current_revision == "retained-pdf"
        assert record.requested_generation == record.completed_generation == 1
        assert _enqueue(db, item_id) == 2
        artifact = db.get(PerformanceSpecificationArtifact, (BOARD, item_id, "retained-pdf"))
        assert artifact.status == "published" and artifact.monday_asset_id == "retained-asset"
        assert record.current_revision == "retained-pdf"


def test_postgres_discovery_recovers_null_policy_identity(postgres_specification):
    _, Session, item_id = postgres_specification
    with Session() as db:
        _enqueue(db, item_id)
        record = db.get(PerformanceSpecification, (BOARD, item_id))
        record.completed_generation = record.requested_generation
        record.checked_at = worker.utc_now()
        record.scheduled_for = worker.utc_now() + timedelta(hours=1)
        record.source_json = {"scopePolicy": SPECIFICATION_SCOPE_POLICY_VERSION,
            "sourcePolicyVersion": None, "referenceParserVersion": REFERENCE_PARSER_VERSION,
            "extractionPipeline": settings.design_processing_pipeline_version,
            "publicationVersion": SPECIFICATION_VERSION}
        db.commit()
        target = SimpleNamespace(board_id=BOARD, item_id=item_id, item_state="active",
            group_id=settings.design_processing_landing_group_id)
        assert worker.discover_specifications(db, gateway=SimpleNamespace(fetch_target=lambda _: target)) == 1
        assert db.get(PerformanceSpecification, (BOARD, item_id)).requested_generation == 2


def test_diagnostic_sql_explains_and_includes_registered_item_without_task(postgres_specification):
    _, Session, item_id = postgres_specification
    now = worker.utc_now()
    query = _live_query(now, [(item_id, settings.design_processing_landing_group_id, "Landing Zone")])
    with Session() as db:
        db.add(DesignProcessingReconciliationCheck(board_id=BOARD, item_id=item_id,
            last_attempted_at=now, last_checked_at=now, last_outcome="ignored", last_reason="ready",
            last_group_id=settings.design_processing_landing_group_id))
        db.commit()
        assert db.query(Task).filter_by(board_id=BOARD, item_id=item_id).count() == 0
        db.rollback()
        db.execute(text("SET TRANSACTION READ ONLY"))
        assert db.execute(text("EXPLAIN " + query)).all()
        db.rollback()
        row = _diagnostic_rows(db, query)[item_id]
        assert row["task_auto_sync_state"] is None
        assert row["design_state"] == "ready_for_review"
        assert row["live_scope_diagnostic"] == "landing_zone_automatic_publication"


def test_diagnostic_sql_uses_only_live_membership_for_scope(postgres_specification):
    _, Session, item_id = postgres_specification
    now = worker.utc_now()
    landing = settings.design_processing_landing_group_id
    completed = settings.auto_sync_completed_group_id
    active = "group_mkpbs35c"
    live_items = [(item_id, active, "Hub B - Outstanding"), ("101", active, "Hub B - Outstanding"),
                  ("102", completed, "Completed Folder"), ("104", "other", "Other")]
    with Session() as db:
        db.add(Task(external_task_key=f"1:{BOARD}:{item_id}", account_id="1", board_id=BOARD,
            item_id=item_id, source_group_id=landing, updated_at=now, auto_sync_state="excluded"))
        db.add(Task(external_task_key=f"1:{BOARD}:105", account_id="1", board_id=BOARD,
            item_id="105", source_group_id=landing, updated_at=now, auto_sync_state="excluded"))
        db.add(DesignProcessingItem(board_id=BOARD, item_id="103", state="ready_for_review"))
        db.flush()
        db.add(PerformanceSpecification(board_id=BOARD, item_id="103"))
        for observed_id, group in ((item_id, "topics"), ("102", "topics"), ("103", landing)):
            observe_item(db, BOARD, {"id": observed_id, "board": {"id": BOARD},
                "group": {"id": group}, "state": "active"},
                access_token="worker", observed_at=now - timedelta(minutes=5))
        observe_unavailable(db, BOARD, item_id, access_token="other-worker", observed_at=now)
        db.commit()

        assert not _diagnostic_rows(db)
        assert not _diagnostic_rows(db, _live_query(now, live_items, board_id="999"))
        live_query = _live_query(now, live_items)
        unscoped = _diagnostic_rows(db, live_query)
        assert unscoped[item_id]["stored_monday_diagnostic"] == "monday_item_unavailable"
        assert unscoped[item_id]["extraction_diagnostic"] == "waiting_for_inputs"
        assert unscoped[item_id]["monday_availability_identity_configured"] is False

        fingerprint = processing_identity("worker")
        query = live_query.replace("NULL::text AS processing_identity", f"'{fingerprint}'::text AS processing_identity")
        query = query.replace("'group_mkqbx92r']", f"'group_mkqbx92r','{completed}']")
        rows = _diagnostic_rows(db, query)
        assert set(rows) == {item_id, "101", "104"}
        assert rows[item_id]["live_group_id"] == active
        assert rows[item_id]["live_group_title"] == "Hub B - Outstanding"
        assert rows[item_id]["live_group_item_count"] == 2
        assert rows[item_id]["source_group_id"] == landing
        assert rows[item_id]["monday_observed_group_id"] == "topics"
        assert rows[item_id]["live_scope_diagnostic"] == "active_group_manual_publication"
        assert rows[item_id]["monday_snapshot_observed_at"] == now
        assert rows[item_id]["monday_availability_identity_configured"] is True
        assert rows["101"]["extraction_diagnostic"] == "item_not_registered"
        assert rows["104"]["live_scope_diagnostic"] == "outside_specification_groups"


def test_diagnostic_sql_exposes_draft_and_checks_full_pipeline(postgres_specification):
    _, Session, item_id = postgres_specification
    live_query = _live_query(worker.utc_now(), [(item_id, "topics", "Hub A - Outstanding")])
    query = live_query.replace(
        "NULL::text AS expected_pipeline_version", "'pipeline-current'::text AS expected_pipeline_version",
    )
    with Session() as db:
        _enqueue(db, item_id)
        item = db.query(DesignProcessingItem).filter_by(board_id=BOARD, item_id=item_id).one()
        item.latest_desired_input_revision, item.latest_analyzed_input_revision = "new-email", "old-email"
        item.latest_desired_pipeline_version, item.latest_analyzed_pipeline_version = "pipeline-current", "pipeline-old"
        spec = db.get(PerformanceSpecification, (BOARD, item_id))
        spec.prepared_revision, spec.current_revision = "draft", "published"
        source = {
            "scopePolicy": SPECIFICATION_SCOPE_POLICY_VERSION,
            "publicationVersion": SPECIFICATION_VERSION,
            "sourcePolicyVersion": SOURCE_POLICY_VERSION,
            "referenceParserVersion": REFERENCE_PARSER_VERSION,
            "extractionPipeline": "pipeline-old",
            "emailRevision": "old-email",
        }
        spec.source_json = source
        for revision, status, artifact_source in (
            ("published", "published", source),
            ("draft", "rendered", {"review": {"publication_identity": "draft-identity"}}),
        ):
            db.add(PerformanceSpecificationArtifact(board_id=BOARD, item_id=item_id, revision=revision,
                translation_json={}, source_json=artifact_source, model_version="test", filename=f"{revision}.pdf",
                storage_bucket="private", storage_object_key=revision, content_sha256="a" * 64,
                size_bytes=1, status=status))
        db.commit()

        row = _diagnostic_rows(db, query)[item_id]
        assert row["prepared_artifact_status"] == "rendered"
        assert row["prepared_publication_identity"] == "draft-identity"
        assert row["current_artifact_status"] == "published"
        assert row["refresh_policy_diagnostic"] == "refresh_policy_obsolete"
        assert row["artifact_freshness_diagnostic"] == "artifact_extraction_obsolete"

        item.latest_desired_input_revision = item.latest_analyzed_input_revision = None
        item.latest_desired_pipeline_version = item.latest_analyzed_pipeline_version = None
        artifact = db.get(PerformanceSpecificationArtifact, (BOARD, item_id, "published"))
        artifact.source_json = {**source, "emailRevision": None, "extractionPipeline": None}
        db.commit()
        assert _diagnostic_rows(db, live_query)[item_id]["artifact_freshness_diagnostic"] == "artifact_source_identity_missing"
