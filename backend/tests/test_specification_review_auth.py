"""Security boundaries for cookie-independent Specification Review V1."""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import jwt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import AppSession, AppUser, HandoffCode, Task, UserMondayLink
from backend.app.routes import monday_auth
from backend.app.services import specification_review_auth as auth


@pytest.fixture(autouse=True)
def configured_review(monkeypatch):
    monkeypatch.setattr(settings, "monday_client_secret", "review-sdk-client-secret")
    monkeypatch.setattr(settings, "monday_signing_secret", "review-server-signing-secret")
    monkeypatch.setattr(settings, "performance_specification_review_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_mode", "enabled")
    monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", ["30"])
    monkeypatch.setattr(settings, "performance_specification_review_session_ttl_seconds", 600)
    monkeypatch.setattr(settings, "performance_specification_review_monday_account_id", "10")
    monkeypatch.setattr(settings, "design_processing_board_id", "20")
    monkeypatch.setattr(settings, "design_processing_landing_group_id", "landing")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")
    monkeypatch.setattr(settings, "design_processing_mode", "enabled")
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", ["30"])
    monkeypatch.setattr(settings, "main_app_base_url", "https://review.example.test")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def sdk_token(**updates):
    claims = {"dat": {"user_id": "40", "account_id": "10"}, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    claims.update(updates)
    return jwt.encode(claims, settings.monday_client_secret, algorithm="HS256")


def bootstrap_payload(token=None, **context):
    return auth.SessionBootstrap(sessionToken=token or sdk_token(), context={"accountId": "10", "boardId": "20", "itemId": "30", **context})


def add_link(db):
    db.add(AppUser(id="app-user", monday_account_id="10", monday_user_id="40"))
    db.flush()
    db.add(UserMondayLink(app_user_id="app-user", monday_account_id="10", monday_user_id="40", access_token="private-oauth-token"))
    db.commit()


def mock_live(monkeypatch, *, group="landing", state="active", item_id="30", board_id="20", user_id="40", account_id="10", readable=True):
    calls = []

    def read(token, query, variables, timeout):
        calls.append((token, query, variables))
        assert token == "private-oauth-token"
        assert "mutation" not in query.lower()
        return {"data": {"me": {"id": user_id, "account": {"id": account_id}}, "items": [{
            "id": item_id, "name": "Review item", "state": state,
            "board": {"id": board_id}, "group": {"id": group},
        }] if readable else []}}

    monkeypatch.setattr(auth.monday_client, "monday_graphql_request", read)
    return calls


def test_v1_bootstrap_and_each_request_verify_user_and_item_without_assigned_role(db, monkeypatch):
    add_link(db)
    calls = mock_live(monkeypatch)
    result = auth.bootstrap_review_session(db, bootstrap_payload())
    assert result["status"] == "ready"
    assert result["capabilities"] == {"read": True, "edit": True, "approve": True}
    assert "private-oauth-token" not in str(result)
    access = auth.authenticate_review(db, "Bearer " + result["access_token"], "20", "30")
    assert access.actor.app_user_id == "app-user"
    assert access.actor.monday_user_id == "40"
    assert access.snapshot["board"]["id"] == "20"
    assert "private-oauth-token" not in repr(access)
    assert len(calls) == 2
    assert not db.in_transaction()
    assert db.query(Task).count() == db.query(HandoffCode).count() == db.query(AppSession).count() == 0


@pytest.mark.parametrize("kind", ["expired", "no_expiry", "wrong_secret", "no_identity"])
def test_rejects_invalid_sdk_identity_before_link_or_live_lookup(db, monkeypatch, kind):
    claims = {"dat": {"user_id": "40", "account_id": "10"}, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    if kind == "expired":
        claims["exp"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    if kind == "no_expiry":
        claims.pop("exp")
    if kind == "no_identity":
        claims["dat"] = {"account_id": "10"}
    token = jwt.encode(claims, "webhook-secret" if kind == "wrong_secret" else settings.monday_client_secret, algorithm="HS256")
    monkeypatch.setattr(auth.monday_client, "monday_graphql_request", lambda *a, **k: pytest.fail("must not contact Monday"))
    with pytest.raises(HTTPException) as exc:
        auth.bootstrap_review_session(db, bootstrap_payload(token))
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "authorization_expired"


def test_sdk_supports_documented_iso_expiry_after_signature_verification(db):
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    result = auth.bootstrap_review_session(db, bootstrap_payload(sdk_token(exp=expiry)))
    assert result["status"] == "oauth_required"


@pytest.mark.parametrize("expiry", ["2000-01-01T00:00:00Z", "2099-01-01T00:00:00", True, None, "not-a-date", float("nan"), float("inf")])
def test_sdk_rejects_expired_ambiguous_or_nonfinite_expiry(db, expiry):
    with pytest.raises(HTTPException) as exc:
        auth.bootstrap_review_session(db, bootstrap_payload(sdk_token(exp=expiry)))
    assert exc.value.status_code == 401


@pytest.mark.parametrize("context", [{"accountId": "11"}, {"boardId": "21"}])
def test_rejects_forged_bootstrap_context(db, context):
    with pytest.raises(HTTPException) as exc:
        auth.bootstrap_review_session(db, bootstrap_payload(**context))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("live", [{"user_id": "41"}, {"account_id": "11"}, {"board_id": "21"}, {"item_id": "31"}, {"readable": False}])
def test_oauth_link_does_not_prove_live_identity_or_item_access(db, monkeypatch, live):
    add_link(db)
    mock_live(monkeypatch, **live)
    with pytest.raises(HTTPException) as exc:
        auth.bootstrap_review_session(db, bootstrap_payload())
    assert exc.value.status_code == 403


def test_missing_oauth_returns_signed_item_bound_top_level_authorization_without_ingestion(db):
    result = auth.bootstrap_review_session(db, bootstrap_payload())
    assert result["status"] == "oauth_required"
    state = parse_qs(urlparse(result["authorization_url"]).query)["state"][0]
    claims = monday_auth._parse_state(state)
    assert claims["mode"] == "specification_review"
    assert claims["monday_account_id"] == "10"
    assert claims["monday_user_id"] == "40"
    assert claims["board_id"] == "20" and claims["item_id"] == "30"
    assert "access_token" not in claims
    assert db.query(Task).count() == db.query(HandoffCode).count() == db.query(AppSession).count() == 0


def test_expired_oauth_link_requires_reconnection(db):
    add_link(db)
    link = db.query(UserMondayLink).one()
    link.token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert auth.bootstrap_review_session(db, bootstrap_payload())["status"] == "oauth_required"


@pytest.mark.parametrize("change, expected", [
    ({"group": "active"}, {"read": True, "edit": False, "approve": False}),
    ({"group": "completed"}, {"read": True, "edit": False, "approve": False}),
    ({"state": "archived"}, {"read": True, "edit": False, "approve": False}),
])
def test_group_or_lifecycle_change_removes_mutation_capabilities(db, monkeypatch, change, expected):
    add_link(db)
    mock_live(monkeypatch)
    token = auth.bootstrap_review_session(db, bootstrap_payload())["access_token"]
    mock_live(monkeypatch, **change)
    access = auth.authenticate_review(db, "Bearer " + token, "20", "30")
    assert access.capabilities == expected


@pytest.mark.parametrize("group", ["landing", ["landing"], [], None, {}])
def test_malformed_or_missing_live_group_fails_closed_to_read_only(db, monkeypatch, group):
    add_link(db)
    monkeypatch.setattr(auth.monday_client, "monday_graphql_request", lambda *a, **kw: {"data": {
        "me": {"id": "40", "account": {"id": "10"}}, "items": [{
            "id": "30", "state": "active", "board": {"id": "20"}, "group": group,
        }],
    }})
    result = auth.bootstrap_review_session(db, bootstrap_payload())
    assert result["capabilities"] == {"read": True, "edit": False, "approve": False}
    assert result["scope_reason"] == "outside_landing_zone"


@pytest.mark.parametrize("setting,value,editable,approvable", [
    ("performance_specification_review_enabled", False, False, False),
    ("performance_specification_mode", "off", False, False),
    ("performance_specification_mode", "shadow", True, False),
    ("performance_specification_enabled", False, True, False),
    ("performance_specification_allowlist_item_ids", ["31"], False, False),
    ("design_processing_mode", "off", True, True),
    ("design_processing_mode", "shadow", True, True),
    ("design_processing_allowlist_item_ids", ["31"], True, True),
])
def test_operating_controls_remain_enforced(db, monkeypatch, setting, value, editable, approvable):
    add_link(db)
    mock_live(monkeypatch)
    if setting == "performance_specification_allowlist_item_ids":
        monkeypatch.setattr(settings, "performance_specification_mode", "allowlist")
    if setting == "design_processing_allowlist_item_ids":
        monkeypatch.setattr(settings, "design_processing_mode", "allowlist")
    monkeypatch.setattr(settings, setting, value)
    result = auth.bootstrap_review_session(db, bootstrap_payload())
    assert result["capabilities"] == {"read": True, "edit": editable, "approve": approvable}


def test_readonly_bearer_needs_bootstrap_to_gain_edit_access(db, monkeypatch):
    add_link(db)
    mock_live(monkeypatch, group="active")
    token = auth.bootstrap_review_session(db, bootstrap_payload())["access_token"]
    mock_live(monkeypatch)
    access = auth.authenticate_review(db, "Bearer " + token, "20", "30")
    assert access.capabilities == {"read": True, "edit": False, "approve": False}


def test_item_bound_bearer_rejects_other_item_and_sdk_token(db, monkeypatch):
    add_link(db)
    calls = mock_live(monkeypatch)
    token = auth.bootstrap_review_session(db, bootstrap_payload())["access_token"]
    with pytest.raises(HTTPException) as exc:
        auth.authenticate_review(db, "Bearer " + token, "20", "31")
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        auth.authenticate_review(db, "Bearer " + sdk_token(), "20", "30")
    assert exc.value.status_code == 401
    assert len(calls) == 1


def test_bearer_expiry_and_revoked_oauth_link_are_revalidated(db, monkeypatch):
    add_link(db)
    mock_live(monkeypatch)
    token = auth.bootstrap_review_session(db, bootstrap_payload())["access_token"]
    claims = jwt.decode(token, auth._session_key(), algorithms=["HS256"], audience=auth.SESSION_AUDIENCE)
    claims["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = jwt.encode(claims, auth._session_key(), algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        auth.authenticate_review(db, "Bearer " + expired, "20", "30")
    assert exc.value.status_code == 401
    db.query(UserMondayLink).delete()
    db.commit()
    with pytest.raises(HTTPException) as exc:
        auth.authenticate_review(db, "Bearer " + token, "20", "30")
    assert exc.value.status_code == 401


@pytest.fixture
def oauth_client(db):
    app = FastAPI()
    app.include_router(monday_auth.router)
    app.dependency_overrides[monday_auth.get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def test_review_oauth_returns_to_review_without_handoff_task_or_cookie_session(db, oauth_client, monkeypatch):
    result = auth.bootstrap_review_session(db, bootstrap_payload())
    state = parse_qs(urlparse(result["authorization_url"]).query)["state"][0]
    monkeypatch.setattr(monday_auth, "_exchange_monday_code", lambda code: {"access_token": "private-oauth-token"})
    monkeypatch.setattr(monday_auth, "_monday_me", lambda token, **kw: {"id": "40", "account": {"id": "10"}})
    mock_live(monkeypatch)
    response = oauth_client.get("/auth/monday/callback", params={"code": "oauth-code", "state": state}, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "https://review.example.test/specification-review?boardId=20&itemId=30"
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert db.query(UserMondayLink).count() == 1
    assert db.query(Task).count() == db.query(HandoffCode).count() == db.query(AppSession).count() == 0
    assert auth.bootstrap_review_session(db, bootstrap_payload())["status"] == "ready"


def test_expired_oauth_link_can_reconnect_with_nonexpiring_replacement(db, oauth_client, monkeypatch):
    add_link(db)
    db.query(UserMondayLink).one().token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    result = auth.bootstrap_review_session(db, bootstrap_payload())
    assert result["status"] == "oauth_required"
    state = parse_qs(urlparse(result["authorization_url"]).query)["state"][0]
    monkeypatch.setattr(monday_auth, "_exchange_monday_code", lambda code: {"access_token": "private-oauth-token"})
    monkeypatch.setattr(monday_auth, "_monday_me", lambda token, **kw: {"id": "40", "account": {"id": "10"}})
    mock_live(monkeypatch)
    response = oauth_client.get("/auth/monday/callback", params={"code": "oauth-code", "state": state}, follow_redirects=False)
    assert response.status_code == 307
    assert db.query(UserMondayLink).one().token_expires_at is None
    assert auth.bootstrap_review_session(db, bootstrap_payload())["status"] == "ready"


@pytest.mark.parametrize("identity", [{"id": "41", "account": {"id": "10"}}, {"id": "40", "account": {"id": "11"}}])
def test_review_oauth_rejects_changed_monday_identity(db, oauth_client, monkeypatch, identity):
    result = auth.bootstrap_review_session(db, bootstrap_payload())
    state = parse_qs(urlparse(result["authorization_url"]).query)["state"][0]
    monkeypatch.setattr(monday_auth, "_exchange_monday_code", lambda code: {"access_token": "private-oauth-token"})
    monkeypatch.setattr(monday_auth, "_monday_me", lambda token, **kw: identity)
    response = oauth_client.get("/auth/monday/callback", params={"code": "oauth-code", "state": state}, follow_redirects=False)
    assert response.status_code == 403
    assert db.query(UserMondayLink).count() == db.query(AppUser).count() == 0


@pytest.mark.parametrize("live", [{"board_id": "21"}, {"item_id": "31"}, {"readable": False}])
def test_review_oauth_rejects_moved_or_unreadable_context_before_linking(db, oauth_client, monkeypatch, live):
    result = auth.bootstrap_review_session(db, bootstrap_payload())
    state = parse_qs(urlparse(result["authorization_url"]).query)["state"][0]
    monkeypatch.setattr(monday_auth, "_exchange_monday_code", lambda code: {"access_token": "private-oauth-token"})
    monkeypatch.setattr(monday_auth, "_monday_me", lambda token, **kw: {"id": "40", "account": {"id": "10"}})
    mock_live(monkeypatch, **live)
    response = oauth_client.get("/auth/monday/callback", params={"code": "oauth-code", "state": state}, follow_redirects=False)
    assert response.status_code == 403
    assert db.query(UserMondayLink).count() == db.query(AppUser).count() == 0
