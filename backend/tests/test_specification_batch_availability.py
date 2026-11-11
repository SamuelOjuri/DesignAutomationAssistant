"""Production specification discovery uses shared, lightweight status batches."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest

from backend.app import monday_client
from backend.app.config import settings
from backend.app.models import (
    DesignProcessingItem, MondayItemAvailability, PerformanceSpecification,
    PerformanceSpecificationArtifact,
)
from backend.app.services import monday_item_availability as availability
from backend.app.services import monday_item_probes as probes
from backend.app.services import performance_specification as worker
from backend.app.services.performance_specification_sources import MondaySpecificationGateway
from backend.tests.test_performance_specification import BOARD, ITEM, case


def status(item_id=ITEM, *, board_id=BOARD, state="active"):
    return {"id": item_id, "state": state, "board": {"id": board_id},
        "group": {"id": settings.design_processing_landing_group_id}}


@pytest.fixture
def production(case, monkeypatch):
    token = "offline-specification-processing-token"
    monkeypatch.setattr(settings, "monday_ingestion_access_token", token)
    calls = []
    current = SimpleNamespace(items={ITEM: status()}, callback=None)

    def fetch_statuses(access_token, item_ids):
        assert access_token == token
        assert not case.db.in_transaction(), "A status batch held a database transaction"
        calls.append(tuple(item_ids))
        if current.callback:
            current.callback()
        return {identity: deepcopy(current.items[identity]) for identity in item_ids if identity in current.items}

    def forbidden(*args, **kwargs):
        pytest.fail("Discovery loaded full source/assets or contacted a live service")

    monkeypatch.setattr(probes, "fetch_monday_item_statuses", fetch_statuses)
    monkeypatch.setattr(MondaySpecificationGateway, "fetch_target", forbidden)
    monkeypatch.setattr(MondaySpecificationGateway, "fetch_holding", forbidden)
    monkeypatch.setattr(MondaySpecificationGateway, "fetch_project", forbidden)
    monkeypatch.setattr(MondaySpecificationGateway, "inspect_files", forbidden)
    monkeypatch.setattr(MondaySpecificationGateway, "upload", forbidden)
    monkeypatch.setattr(MondaySpecificationGateway, "delete", forbidden)
    monkeypatch.setattr(monday_client, "monday_graphql_request", forbidden)
    return SimpleNamespace(case=case, gateway=MondaySpecificationGateway(token), token=token,
        calls=calls, response=current)


def discover(production, **kwargs):
    return worker.discover_specifications(production.case.db, gateway=production.gateway, **kwargs)


def retained_specification(production):
    db = production.case.db
    row = PerformanceSpecification(board_id=BOARD, item_id=ITEM, requested_generation=1, completed_generation=1,
        scheduled_for=worker.utc_now() - timedelta(minutes=1), attempt_count=0, current_revision="retained-revision")
    db.add(row)
    db.flush()
    artifact = PerformanceSpecificationArtifact(board_id=BOARD, item_id=ITEM, revision="retained-revision",
        translation_json={"retained": "reviewed values"}, source_json={"retained": "source evidence"},
        model_version="retained-model", filename="retained.pdf", storage_bucket="private",
        storage_object_key="retained.pdf", content_sha256="retained-digest", size_bytes=100,
        monday_asset_id="retained-asset", status="published")
    db.add(artifact)
    db.commit()
    return row, artifact


def test_production_discovery_enqueues_eligible_items_with_one_cheap_batch(production):
    case = production.case
    case.db.add(DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id="124", state="ready_for_review", warnings_json=[]))
    case.db.commit()
    production.response.items["124"] = status("124")
    assert discover(production) == 2
    assert production.calls == [(ITEM, "124")]
    rows = case.db.query(PerformanceSpecification).order_by(PerformanceSpecification.item_id).all()
    assert [row.item_id for row in rows] == [ITEM, "124"]
    assert all(row.requested_generation == 1 and row.completed_generation == 0 and row.scheduled_for is not None for row in rows)
    assert case.db.query(PerformanceSpecificationArtifact).count() == 0
    assert not case.calls and not case.gateway.uploads
    assert discover(production) == 0
    assert production.calls == [(ITEM, "124")]


def test_missing_item_records_shared_backoff_and_prevents_the_next_batch(production):
    production.response.items = {}
    assert discover(production) == 0
    observed = production.case.db.query(MondayItemAvailability).one()
    assert observed.state == "unavailable" and observed.consecutive_misses == 1
    assert observed.next_check_at is not None
    assert production.case.db.get(PerformanceSpecification, (BOARD, ITEM)) is None
    assert discover(production) == 0
    assert production.calls == [(ITEM,)]
    production.case.db.refresh(observed)
    assert observed.consecutive_misses == 1
    assert production.case.item.state == "ready_for_review"


@pytest.mark.parametrize("lifecycle,board_id,reason", [
    ("active", "999", "board_not_managed"),
    ("archived", BOARD, "item_archived"),
    ("deleted", BOARD, "item_deleted"),
])
def test_positive_out_of_scope_status_cancels_old_specification_and_retains_pdf(production, lifecycle, board_id, reason):
    row, artifact = retained_specification(production)
    production.response.items[ITEM] = status(board_id=board_id, state=lifecycle)
    assert discover(production) == 0
    production.case.db.refresh(row)
    production.case.db.refresh(artifact)
    assert row.scheduled_for is None and row.lease_token is None
    assert row.requested_generation == row.completed_generation == 1
    assert row.review_json["status"] == "read_only" and row.review_json["reason"] == reason
    assert row.current_revision == artifact.revision == "retained-revision"
    assert artifact.status == "published" and artifact.monday_asset_id == "retained-asset"
    assert artifact.translation_json == {"retained": "reviewed values"}
    assert discover(production) == 0
    assert production.calls == [(ITEM,)]


@pytest.mark.parametrize("old_lifecycle,old_board", [("active", "999"), ("archived", BOARD)])
def test_restore_after_batch_completion_prevents_stale_scope_cancellation(production, monkeypatch, old_lifecycle, old_board):
    row, artifact = retained_specification(production)
    production.response.items[ITEM] = status(board_id=old_board, state=old_lifecycle)
    real_probe = probes.probe_items

    def restore_after_probe(db, *args, **kwargs):
        result = real_probe(db, *args, **kwargs)
        # This models a webhook commit after the batch's accepted observation,
        # but before discovery consumes the returned earlier status payload.
        availability.observe_item(db, BOARD, status(), access_token=production.token,
            observed_at=availability.utc_now() + timedelta(seconds=1))
        db.commit()
        return result

    monkeypatch.setattr(probes, "probe_items", restore_after_probe)
    assert discover(production) == 0
    production.case.db.refresh(row)
    production.case.db.refresh(artifact)
    assert row.scheduled_for is not None and row.review_json is None
    assert row.current_revision == "retained-revision" and artifact.status == "published"
    observed = production.case.db.query(MondayItemAvailability).one()
    assert observed.state == "available" and observed.actual_board_id == BOARD
    assert production.calls == [(ITEM,)]
