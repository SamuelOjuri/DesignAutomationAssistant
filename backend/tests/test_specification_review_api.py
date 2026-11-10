"""HTTP boundaries for the independent, version 1 Specification Review API.

External identity and source reads are isolated at their service boundaries. The
save journey below uses the real persistence transaction to exercise retries and
competing reviewer identities through HTTP without Monday business mutations.
"""

from copy import deepcopy
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import (
    DesignProcessingItem,
    PerformanceSpecification,
    PerformanceSpecificationReviewEvent,
    Task,
    TaskSnapshot,
)
from backend.app.routes import specification_review as review_api
from backend.app.services import specification_review as persistence
from backend.app.services.specification_review_contract import (
    FIELD_NAMES,
    ReviewActor,
    ReviewSourceSnapshot,
)


BOARD, ITEM, ACCOUNT = "1882196103", "123", "200"
BASE = f"/api/specification-review/items/{BOARD}/{ITEM}"
HEADERS = {"Authorization": "Bearer review-session"}
ACTOR = ReviewActor(app_user_id="reviewer-1", monday_user_id="100", monday_account_id=ACCOUNT)


def source_snapshot():
    citation = {"source_id": "email:body", "excerpt": "A new scheme", "location": "email body"}
    fields = {}
    for label in FIELD_NAMES:
        proposal = {
            "proposal_id": f"sources-1:{label}", "display_value": f"Requested {label}",
            "source": "email_context", "rationale": "Current client request",
            "email_evidence": [citation], "cad_mapping": {"status": "unmapped"},
        }
        if label in {"Drawing Reference", "Revision", "Incoming Information"}:
            proposal.update(
                display_value="" if label == "Incoming Information" else None,
                source="default_blank" if label == "Incoming Information" else "unassigned",
                email_evidence=[], cad_mapping={"status": "not_applicable"},
            )
        fields[label] = {"proposal": proposal}
    return ReviewSourceSnapshot.model_validate({
        "identity": {
            "source_fingerprint": "sources-1", "extraction_identity": "extraction-1",
            "source_policy_version": "specification-review-source-v1",
            "reference_parser_version": "test-parser-v1",
        },
        "intent": {"recommended_type": "new_enquiry", "explanation": "A new scheme", "evidence": [citation]},
        "request_type": "new_enquiry", "fields": fields,
        "candidates": {"status": "available", "projects": [], "drawings": []},
    })


def decisions(**changes):
    return {
        "expected_review_version": 0,
        "expected_source_fingerprint": "sources-1",
        "idempotency_key": "save-request-1",
        "field_operations": {"Decking": {"operation": "specify_blank"}},
        **changes,
    }


def approval(**changes):
    return {
        "expected_review_version": 1,
        "expected_source_fingerprint": "sources-1",
        "idempotency_key": "approval-request-1",
        "artifact_revision": "published-revision-1",
        "expected_decision_digest": "decisions-1",
        "expected_publication_identity": "publication-1",
        **changes,
    }


@pytest.fixture()
def api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory() as db:
        access = SimpleNamespace(
            actor=ACTOR, account_id=ACCOUNT, board_id=BOARD, item_id=ITEM,
            access_token="private-user-oauth-token",
            capabilities={"read": True, "edit": True, "approve": True},
            snapshot={"id": ITEM, "board": {"id": BOARD}, "group": {"id": "landing"}, "state": "active"},
            scope_reason=None,
        )
        calls = []
        gateway = object()

        def authenticate(db, authorization, board_id, item_id):
            calls.append((authorization, board_id, item_id))
            if authorization != HEADERS["Authorization"]:
                raise HTTPException(status_code=401, detail={
                    "code": "authorization_expired", "message": "Renew the Monday review session",
                })
            if (board_id, item_id) != (BOARD, ITEM):
                raise HTTPException(status_code=403, detail={
                    "code": "reviewer_forbidden", "message": "Session belongs to a different item",
                })
            return access

        monkeypatch.setattr(review_api, "authenticate_review", authenticate)
        monkeypatch.setattr(review_api, "_gateway", lambda: gateway)
        app = FastAPI()
        app.include_router(review_api.router)
        app.dependency_overrides[review_api.get_db] = lambda: db
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, db=db, access=access, calls=calls, gateway=gateway)
    engine.dispose()


def _assert_private(response):
    assert "no-store" in response.headers.get("cache-control", "")


def test_sdk_bootstrap_uses_review_identity_without_task_ingestion(api, monkeypatch):
    observed = []

    def bootstrap(db, payload):
        observed.append(payload.model_dump())
        return {"status": "ready", "token": "review-bearer", "capabilities": api.access.capabilities}

    monkeypatch.setattr(review_api, "bootstrap_review_session", bootstrap)
    payload = {"sessionToken": "signed-sdk-token", "context": {"accountId": ACCOUNT, "boardId": BOARD, "itemId": ITEM}}
    response = api.client.post("/api/specification-review/session", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert observed == [payload]
    assert api.db.query(Task).count() == 0
    assert api.db.query(TaskSnapshot).count() == 0
    _assert_private(response)


def test_bootstrap_can_request_review_specific_oauth_without_handoff(api, monkeypatch):
    monkeypatch.setattr(review_api, "bootstrap_review_session", lambda db, payload: {
        "status": "oauth_required", "authorization_url": "https://auth.monday.com/oauth2/authorize?state=review-state",
    })
    response = api.client.post("/api/specification-review/session", json={
        "sessionToken": "signed-sdk-token", "context": {"accountId": ACCOUNT, "boardId": BOARD, "itemId": ITEM},
    })
    assert response.status_code == 200
    assert response.json()["status"] == "oauth_required"
    assert api.db.query(Task).count() == 0
    _assert_private(response)


def test_invalid_bootstrap_never_echoes_sdk_credentials(api):
    secret = "sensitive-sdk-token-must-not-appear-in-response"
    response = api.client.post("/api/specification-review/session", json={
        "sessionToken": secret, "context": {"accountId": ACCOUNT, "boardId": BOARD},
    })
    assert response.status_code == 422
    assert secret not in response.text
    assert response.json()["detail"]["code"] == "invalid_request"
    _assert_private(response)


def test_workspace_opens_without_task_or_snapshot_and_passes_verified_identity(api, monkeypatch):
    observed = []

    def get_workspace(db, **kwargs):
        observed.append(kwargs)
        return {"status": "needs_review", "fields": list(FIELD_NAMES), "candidates": {"status": "available"}}

    monkeypatch.setattr(review_api.workspace, "get_workspace", get_workspace)
    response = api.client.get(BASE, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["fields"] == list(FIELD_NAMES)
    assert observed == [{"board_id": BOARD, "item_id": ITEM, "account_id": ACCOUNT, "gateway": api.gateway, "editable": True}]
    assert api.db.query(Task).count() == 0
    assert api.db.query(TaskSnapshot).count() == 0
    _assert_private(response)


@pytest.mark.parametrize("suffix", ["", "/status", "/candidates", "/pdf/revision-1"])
def test_protected_reads_require_bearer_identity(api, suffix):
    response = api.client.get(BASE + suffix)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authorization_expired"
    _assert_private(response)


def test_item_bound_session_cannot_open_another_item(api):
    response = api.client.get(f"/api/specification-review/items/{BOARD}/456", headers=HEADERS)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "reviewer_forbidden"


def test_lost_read_access_blocks_pdf_before_storage_lookup(api, monkeypatch):
    api.access.capabilities["read"] = False

    def unexpected(*args, **kwargs):
        raise AssertionError("Lost item access must prevent private artifact reads")

    monkeypatch.setattr(review_api.workspace, "read_pdf", unexpected)
    response = api.client.get(BASE + "/pdf/revision-1", headers=HEADERS)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "reviewer_forbidden"
    _assert_private(response)


@pytest.mark.parametrize("method,suffix,payload", [
    ("put", "/decisions", decisions()),
    ("post", "/refresh", {key: value for key, value in decisions().items() if key != "field_operations"}),
    ("post", "/approve", approval()),
])
@pytest.mark.parametrize("reason,code,status", [
    ("outside_landing_zone", "scope_changed", 409),
    ("review_disabled", "reviewer_forbidden", 403),
])
def test_read_only_or_disabled_session_cannot_mutate(api, monkeypatch, method, suffix, payload, reason, code, status):
    api.access.capabilities.update(edit=False, approve=False)
    api.access.scope_reason = reason

    def unexpected(*args, **kwargs):
        raise AssertionError("A read-only actor must never reach a mutation service")

    for name in ("save_decisions", "refresh_recommendations", "approve_specification"):
        monkeypatch.setattr(review_api.workspace, name, unexpected)
    response = getattr(api.client, method)(BASE + suffix, headers=HEADERS, json=payload)
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    _assert_private(response)


def test_save_retry_and_competing_reviewer_return_original_receipt_or_conflict(api, monkeypatch):
    db = api.db
    db.add(DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id=ITEM, state="ready_for_review"))
    db.flush()
    db.add(PerformanceSpecification(board_id=BOARD, item_id=ITEM, requested_generation=0, completed_generation=0))
    db.commit()
    source = source_snapshot()
    persistence.initialize_review(db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT, source=source)

    def save(db, *, board_id, item_id, actor, command, gateway):
        assert gateway is api.gateway
        return persistence.save_review_decisions(db, board_id=board_id, item_id=item_id, actor=actor, command=command, source=source)

    monkeypatch.setattr(review_api.workspace, "save_decisions", save)
    first = api.client.put(BASE + "/decisions", headers=HEADERS, json=decisions())
    assert first.status_code in {200, 202}
    assert first.json()["review_version"] == 1
    retry = api.client.put(BASE + "/decisions", headers=HEADERS, json=decisions())
    assert retry.status_code == first.status_code
    assert retry.json() == first.json()

    api.access.actor = ReviewActor(app_user_id="another-user", monday_user_id="101", monday_account_id=ACCOUNT)
    stale = api.client.put(BASE + "/decisions", headers=HEADERS, json=decisions(idempotency_key="second-reviewer"))
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_review"
    receipts = db.query(PerformanceSpecificationReviewEvent).filter_by(command_type="save_decisions").all()
    assert len(receipts) == 1
    assert receipts[0].actor_monday_user_id == ACTOR.monday_user_id
    assert db.query(Task).count() == 0
    _assert_private(stale)


@pytest.mark.parametrize("extra", [
    {"actor": {"app_user_id": "forged", "monday_user_id": "999", "monday_account_id": ACCOUNT}},
    {"source_labels": {"Decking": "Email Context"}},
    {"field_operations": {"Decking": {"operation": "specify_value", "display_value": "Timber", "cad_mapping": {"status": "mapped", "value": "Timber"}}}},
])
def test_untrusted_save_cannot_supply_actor_provenance_or_mapping(api, extra):
    response = api.client.put(BASE + "/decisions", headers=HEADERS, json=decisions(**extra))
    assert response.status_code == 422


@pytest.mark.parametrize("missing", ["expected_review_version", "expected_source_fingerprint", "idempotency_key"])
def test_save_requires_explicit_concurrency_and_retry_identity(api, missing):
    payload = decisions()
    payload.pop(missing)
    response = api.client.put(BASE + "/decisions", headers=HEADERS, json=payload)
    assert response.status_code == 422


def test_approve_passes_exact_artifact_identity_and_trusted_actor(api, monkeypatch):
    observed = []

    def approve(db, **kwargs):
        observed.append(kwargs)
        return {"review_version": 1, "approval_id": "approval-1", "changed": True}

    monkeypatch.setattr(review_api.workspace, "approve_specification", approve)
    response = api.client.post(BASE + "/approve", headers=HEADERS, json=approval())
    assert response.status_code == 200
    assert response.json()["approval_id"] == "approval-1"
    assert observed[0]["actor"] == ACTOR
    assert observed[0]["command"].artifact_revision == "published-revision-1"
    assert observed[0]["command"].expected_decision_digest == "decisions-1"
    assert observed[0]["command"].expected_publication_identity == "publication-1"


@pytest.mark.parametrize("missing", ["artifact_revision", "expected_decision_digest", "expected_publication_identity"])
def test_approval_requires_exact_publication_identity(api, missing):
    payload = approval()
    payload.pop(missing)
    response = api.client.post(BASE + "/approve", headers=HEADERS, json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("code,status", [
    ("stale_review", 409), ("source_changed", 409), ("idempotency_conflict", 409),
    ("source_waiting", 202), ("source_unavailable", 503), ("publication_failed", 502),
])
def test_operation_errors_have_stable_codes_and_private_responses(api, monkeypatch, code, status):
    def fail(*args, **kwargs):
        raise persistence.ReviewConflict(code, "Actionable review error")

    monkeypatch.setattr(review_api.workspace, "save_decisions", fail)
    response = api.client.put(BASE + "/decisions", headers=HEADERS, json=decisions())
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    _assert_private(response)


def test_status_polling_does_not_request_gateway_or_rebuild_workspace(api, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Status polling must not fetch sources or rebuild the workspace")

    monkeypatch.setattr(review_api, "_gateway", unexpected)
    monkeypatch.setattr(review_api.workspace, "get_workspace", unexpected)
    monkeypatch.setattr(review_api.workspace, "get_status", lambda db, **kwargs: {
        "status": "regenerating_pdf", "review_version": 3, "source_fingerprint": "current-sources",
    })
    response = api.client.get(BASE + "/status", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "regenerating_pdf"
    _assert_private(response)


def test_candidates_returns_only_candidate_payload(api, monkeypatch):
    candidates = {"status": "available", "projects": [{"record": {"board_id": "400", "item_id": "500"}}], "drawings": []}
    monkeypatch.setattr(review_api.workspace, "get_workspace", lambda db, **kwargs: {
        "status": "needs_review", "fields": {"Company": "private"}, "candidates": deepcopy(candidates),
        "review_version": 2, "source_fingerprint": "candidate-sources",
    })
    response = api.client.get(BASE + "/candidates", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "candidates": candidates, "review_version": 2, "source_fingerprint": "candidate-sources",
    }


def test_upstream_errors_do_not_expose_transport_urls_or_tokens(api, monkeypatch):
    def unavailable(*args, **kwargs):
        raise HTTPException(status_code=503, detail="Failed https://private.example.test/file?token=secret-token")

    monkeypatch.setattr(review_api.workspace, "get_status", unavailable)
    response = api.client.get(BASE + "/status", headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "source_unavailable"
    assert "secret-token" not in response.text
    assert "private.example.test" not in response.text
    _assert_private(response)


def test_post_move_retained_pdf_remains_private_and_readable(api, monkeypatch):
    api.access.capabilities.update(edit=False, approve=False)
    api.access.scope_reason = "outside_landing_zone"
    observed = []

    def read_pdf(db, **kwargs):
        observed.append(kwargs)
        return "performance-specification-retained.pdf", b"%PDF-1.4\nretained"

    monkeypatch.setattr(review_api.workspace, "read_pdf", read_pdf)
    response = api.client.get(BASE + "/pdf/retained-revision", headers=HEADERS)
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-1.4")
    assert response.headers["content-type"] == "application/pdf"
    assert "performance-specification-retained.pdf" in response.headers["content-disposition"]
    assert observed == [{"board_id": BOARD, "item_id": ITEM, "account_id": ACCOUNT, "revision": "retained-revision"}]
    _assert_private(response)
