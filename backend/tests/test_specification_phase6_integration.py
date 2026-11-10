"""HTTP journeys with real auth, review orchestration and persistence.

Only Monday reads, private storage and publisher output are simulated. No task,
handoff, model, extraction or Monday mutation capability is used by the review.
"""

from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import jwt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import (
    AppUser, DesignProcessingItem, HandoffCode, PerformanceSpecification,
    PerformanceSpecificationArtifact, PerformanceSpecificationReview,
    PerformanceSpecificationReviewEvent, Task, TaskSnapshot, UserMondayLink,
)
from backend.app.routes import specification_review as api
from backend.app.services import specification_review as persistence
from backend.app.services import specification_review_auth as auth
from backend.app.services import specification_review_workspace as workspace
from backend.app.services.specification_review_artifacts import build_review_artifact_metadata
from backend.tests.test_specification_review_api import ACCOUNT, BOARD, FIELD_NAMES, ITEM, source_snapshot


BASE = f"/api/specification-review/items/{BOARD}/{ITEM}"


@pytest.fixture()
def journey(monkeypatch):
    monkeypatch.setattr(settings, "monday_client_secret", "phase6-sdk-secret")
    monkeypatch.setattr(settings, "monday_signing_secret", "phase6-server-secret")
    monkeypatch.setattr(settings, "design_processing_board_id", BOARD)
    monkeypatch.setattr(settings, "design_processing_landing_group_id", "landing")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")
    monkeypatch.setattr(settings, "performance_specification_mode", "enabled")
    monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", [])
    monkeypatch.setattr(settings, "performance_specification_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_review_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_review_monday_account_id", ACCOUNT)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        for user_id in ("100", "101"):
            app_user_id = f"phase6-user-{user_id}"
            db.add(AppUser(id=app_user_id, monday_account_id=ACCOUNT, monday_user_id=user_id))
            db.flush()
            db.add(UserMondayLink(app_user_id=app_user_id, monday_account_id=ACCOUNT,
                                  monday_user_id=user_id, access_token=f"oauth-{user_id}"))
        db.add(DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id=ITEM,
                                   state="ready_for_review"))
        db.commit()
        current = SimpleNamespace(source=source_snapshot(), group="landing", readable=True,
                                  assets=[], source_reads=0, live_reads=0)

        def monday_read(token, query, variables, timeout):
            assert not db.in_transaction()
            assert "mutation" not in query.lower()
            assert token in {"oauth-100", "oauth-101"}
            current.live_reads += 1
            return {"data": {
                "me": {"id": token.split("-")[1], "account": {"id": ACCOUNT}},
                "items": [{"id": ITEM, "name": "New roof", "state": "active",
                           "board": {"id": BOARD}, "group": {"id": current.group}}]
                         if current.readable else [],
            }}

        def resolve(*args, **kwargs):
            assert not db.in_transaction()
            current.source_reads += 1
            return {"reviewSource": current.source.model_dump(mode="json")}

        class Gateway:
            def inspect_files(self, item_id):
                assert not db.in_transaction()
                assert item_id == ITEM
                return current.assets

        class Storage:
            def read_private(self, bucket, key):
                assert not db.in_transaction()
                assert (bucket, key) == ("phase6-private", "current.pdf")
                return b"%PDF-1.7\nPhase 6 reviewed PDF"

        monkeypatch.setattr(auth.monday_client, "monday_graphql_request", monday_read)
        monkeypatch.setattr(workspace, "resolve_specification_sources", resolve)
        monkeypatch.setattr(workspace, "SupabaseDesignArtifactStorage", Storage)
        monkeypatch.setattr(api, "_gateway", Gateway)
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[api.get_db] = lambda: db
        with TestClient(app) as client:
            yield SimpleNamespace(db=db, client=client, current=current, content=Storage().read_private(
                "phase6-private", "current.pdf"))
    engine.dispose()


def bootstrap(journey, user_id="100"):
    token = jwt.encode({"dat": {"account_id": ACCOUNT, "user_id": user_id},
                        "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                       settings.monday_client_secret, algorithm="HS256")
    response = journey.client.post("/api/specification-review/session", json={
        "sessionToken": token, "context": {"accountId": ACCOUNT, "boardId": BOARD, "itemId": ITEM},
    })
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"] == {"read": True, "edit": True, "approve": True}
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def corrections(view, key="save-corrections"):
    return {
        "expected_review_version": view["review_version"],
        "expected_source_fingerprint": view["source_fingerprint"], "idempotency_key": key,
        "selections": {"selected_request_type": "new_enquiry"},
        "field_operations": {label: {"operation": "specify_blank"} for label in FIELD_NAMES},
    }


def publish_matching_pdf(journey):
    db = journey.db
    review = db.get(PerformanceSpecificationReview, (BOARD, ITEM))
    metadata = build_review_artifact_metadata(persistence.review_state(review), journey.current.source)
    artifact = PerformanceSpecificationArtifact(
        board_id=BOARD, item_id=ITEM, revision="phase6-current", filename="current.pdf",
        source_json={"review": metadata, "reviewSource": journey.current.source.model_dump(mode="json")},
        translation_json={}, model_version="phase6-test", storage_bucket="phase6-private",
        storage_object_key="current.pdf", content_sha256=hashlib.sha256(journey.content).hexdigest(),
        size_bytes=len(journey.content), monday_asset_id="900", status="published",
    )
    db.add(artifact)
    spec = db.get(PerformanceSpecification, (BOARD, ITEM))
    spec.current_revision = artifact.revision
    spec.completed_generation = spec.requested_generation
    spec.lease_token = spec.lease_until = None
    db.commit()
    journey.current.assets = [SimpleNamespace(asset_id="900", filename="current.pdf", size_bytes=len(journey.content))]
    return {
        "expected_review_version": review.review_version,
        "expected_source_fingerprint": review.source_fingerprint, "idempotency_key": "approve-current",
        "artifact_revision": artifact.revision, "expected_decision_digest": metadata["decision_digest"],
        "expected_publication_identity": metadata["publication_identity"],
    }


def test_review_save_publish_approve_move_and_source_change_journey(journey):
    headers = bootstrap(journey)
    view = journey.client.get(BASE, headers=headers).json()
    assert len(view["fields"]) == 18 and view["review_version"] == 0
    saved = journey.client.put(BASE + "/decisions", headers=headers, json=corrections(view))
    assert saved.status_code == 200 and saved.json()["review_version"] == 1
    assert journey.client.get(BASE + "/status", headers=headers).json()["status"] == "regenerating_pdf"

    approval = publish_matching_pdf(journey)
    preview = journey.client.get(BASE + "/pdf/phase6-current", headers=headers)
    assert preview.status_code == 200 and preview.content == journey.content
    completed = journey.client.post(BASE + "/approve", headers=headers, json=approval)
    assert completed.status_code == 200 and completed.json()["approval_id"]
    assert journey.client.get(BASE + "/status", headers=headers).json()["status"] == "complete"

    journey.current.group = "active-design"
    moved = journey.client.get(BASE, headers=headers)
    assert moved.status_code == 200 and moved.json()["status"] == "read_only"
    assert moved.json()["capabilities"]["edit"] is False
    assert journey.client.post(BASE + "/approve", headers=headers, json=approval).status_code == 409
    assert journey.client.get(BASE + "/pdf/phase6-current", headers=headers).content == journey.content

    journey.current.group = "landing"
    journey.current.source = journey.current.source.model_copy(deep=True)
    journey.current.source.identity.source_fingerprint = "sources-2"
    changed = journey.client.get(BASE, headers=headers).json()
    assert changed["source_fingerprint"] == "sources-2" and changed["approval"] is None
    assert changed["fields"]["Decking"]["effective"]["display_value"] == ""
    assert changed["fields"]["Decking"]["effective"]["requires_reconfirmation"] is True
    assert changed["published_pdf"]["revision"] == "phase6-current"
    assert journey.db.query(Task).count() == journey.db.query(TaskSnapshot).count() == journey.db.query(HandoffCode).count() == 0


def test_two_authenticated_monday_users_can_review_with_safe_conflicts_and_retries(journey):
    first, second = bootstrap(journey, "100"), bootstrap(journey, "101")
    view = journey.client.get(BASE, headers=first).json()
    command = corrections(view)
    original = journey.client.put(BASE + "/decisions", headers=first, json=command)
    assert original.status_code == 200
    conflict = journey.client.put(BASE + "/decisions", headers=second, json=corrections(view, key="second-user-save"))
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "stale_review"
    retry = journey.client.put(BASE + "/decisions", headers=first, json=command)
    assert retry.status_code == 200 and retry.json() == original.json()
    events = journey.db.query(PerformanceSpecificationReviewEvent).filter_by(command_type="save_decisions").all()
    assert len(events) == 1 and events[0].actor_monday_user_id == "100"
    journey.db.rollback()
    journey.current.readable = False
    denied = journey.client.get(BASE, headers=first)
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "reviewer_forbidden"
