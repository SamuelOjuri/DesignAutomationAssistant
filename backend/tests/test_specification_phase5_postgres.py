"""Optional real PostgreSQL lifecycle, queue fencing and diagnostic SQL checks.

Set TEST_DATABASE_URL to a disposable PostgreSQL database with pgvector. The
shared fixture migrates and drops only a UUID-named isolated schema. No Monday,
model, storage or configured application database connection is used.
"""

from datetime import timedelta
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
from backend.app.services.performance_specification_contract import SPECIFICATION_VERSION
from backend.app.services.specification_policy import SPECIFICATION_SCOPE_POLICY_VERSION
from backend.app.services.specification_references import REFERENCE_PARSER_VERSION
from backend.tests.test_specification_review_postgres_concurrency import postgres_review_database, race


BOARD = "1882196103"


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
    with Session() as db:
        db.add(DesignProcessingReconciliationCheck(board_id=BOARD, item_id=item_id,
            last_attempted_at=now, last_checked_at=now, last_outcome="ignored", last_reason="ready",
            last_group_id=settings.design_processing_landing_group_id))
        db.commit()
        assert db.query(Task).filter_by(board_id=BOARD, item_id=item_id).count() == 0
        db.rollback()
        db.execute(text("SET TRANSACTION READ ONLY"))
        query = (Path(__file__).resolve().parents[1] / "scripts" /
            "diagnose_performance_specifications.sql").read_text(encoding="utf-8")
        assert db.execute(text("EXPLAIN " + query)).all()
        rows = db.execute(text(query)).mappings().all()
        row = next(row for row in rows if row["board_id"] == BOARD and row["item_id"] == item_id)
        assert row["task_auto_sync_state"] is None
        assert row["design_state"] == "ready_for_review"
        assert row["stored_scope_diagnostic"] == "landing_zone_check_live_lifecycle"
