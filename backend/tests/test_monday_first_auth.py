from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import unescape
import re
from urllib.parse import parse_qs, urlparse
import uuid

import jwt
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import AppSession, AppUser, HandoffCode, Task, TaskFile, TaskSnapshot, UserMondayLink
from backend.app.routes import chat, monday_auth, monday_handoff, tasks
from backend.app.monday_client import MONDAY_API_URL, MONDAY_TOKEN_URL


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def monday_first_settings(monkeypatch):
    monkeypatch.setattr(settings, "monday_client_id", "client-id")
    monkeypatch.setattr(settings, "monday_client_secret", "client-secret")
    monkeypatch.setattr(settings, "monday_signing_secret", "signing-secret")
    monkeypatch.setattr(settings, "backend_base_url", "https://api.example.test")
    monkeypatch.setattr(settings, "main_app_base_url", "https://app.example.test")
    monkeypatch.setattr(settings, "monday_oauth_redirect_uri", None)
    monkeypatch.setattr(settings, "app_session_cookie_secure", False)
    monkeypatch.setattr(settings, "app_session_cookie_samesite", "lax")
    monkeypatch.setattr(settings, "app_session_cookie_domain", None)
    monkeypatch.setattr(settings, "app_session_max_age_seconds", 3600)


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(monday_auth.router)
    app.include_router(monday_handoff.router)
    app.include_router(tasks.router)
    app.include_router(chat.router)

    app.dependency_overrides[monday_auth.get_db] = lambda: db_session
    app.dependency_overrides[monday_handoff.get_db] = lambda: db_session
    app.dependency_overrides[tasks.get_db] = lambda: db_session
    app.dependency_overrides[chat.get_db] = lambda: db_session

    with TestClient(app) as test_client:
        yield test_client


class FakeResponse:
    def __init__(self, payload: dict, *, ok: bool = True, status_code: int = 200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


def _add_handoff_code(db_session, *, code: str = "handoff-code", user_id: str = "monday-user") -> HandoffCode:
    handoff_code = HandoffCode(
        code=code,
        monday_account_id="acct",
        monday_board_id="board-1",
        monday_item_id="item-1",
        monday_user_id=user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        used=False,
    )
    db_session.add(handoff_code)
    db_session.commit()
    return handoff_code


def _mock_monday_oauth(
    monkeypatch,
    *,
    user_id: str = "monday-user",
    account_id: str = "acct",
    email: str | None = None,
    name: str = "Monday User",
) -> None:
    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        if url == MONDAY_TOKEN_URL:
            return FakeResponse({"access_token": "monday-token", "expires_in": 3600})
        if url == MONDAY_API_URL:
            return FakeResponse(
                {
                    "data": {
                        "me": {
                            "id": user_id,
                            "name": name,
                            "email": email,
                            "account": {"id": account_id},
                        }
                    }
                }
            )
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(monday_auth.requests, "post", fake_post)


def _monday_first_state_from_login(client: TestClient, code: str) -> str:
    response = client.get(
        "/auth/monday/login",
        params={
            "mode": "monday_first",
            "handoff_code": code,
            "return_to": f"/monday-handoff/{code}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 307

    location = response.headers["location"]
    assert location.startswith("https://auth.monday.com/oauth2/authorize")
    state = parse_qs(urlparse(location).query)["state"][0]
    payload = jwt.decode(state, settings.monday_signing_secret, algorithms=["HS256"])
    assert payload["mode"] == "monday_first"
    assert payload["handoff_code"] == code
    return state


def _complete_monday_oauth(client: TestClient, state: str):
    return client.get(
        "/auth/monday/callback",
        params={"code": "oauth-code", "state": state},
        follow_redirects=False,
    )


def _csrf_headers(client: TestClient) -> dict[str, str]:
    csrf_token = client.cookies.get(settings.app_csrf_cookie_name)
    assert csrf_token
    return {"X-CSRF-Token": csrf_token}


def _monday_session_token(*, user_id: str = "monday-user", account_id: str = "acct") -> str:
    claims = {
        "dat": {
            "user_id": user_id,
            "account_id": account_id,
        },
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(claims, settings.monday_client_secret, algorithm="HS256")


def test_handoff_init_accepts_session_token_signed_with_monday_client_secret(client, db_session):
    response = client.post(
        "/api/monday/handoff/init",
        json={
            "sessionToken": _monday_session_token(),
            "context": {
                "accountId": "acct",
                "boardId": "board-1",
                "itemId": "item-1",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "/monday-handoff/" in body["url"]

    handoff_code = db_session.get(HandoffCode, body["code"])
    assert handoff_code is not None
    assert handoff_code.monday_account_id == "acct"
    assert handoff_code.monday_user_id == "monday-user"


def test_handoff_init_rejects_session_token_signed_with_monday_signing_secret(client):
    token = jwt.encode(
        {
            "dat": {"user_id": "monday-user", "account_id": "acct"},
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        settings.monday_signing_secret,
        algorithm="HS256",
    )

    response = client.post(
        "/api/monday/handoff/init",
        json={
            "sessionToken": token,
            "context": {
                "accountId": "acct",
                "boardId": "board-1",
                "itemId": "item-1",
            },
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid monday session token"


def test_monday_first_oauth_callback_creates_app_user_link_and_session(client, db_session, monkeypatch):
    _add_handoff_code(db_session)
    state = _monday_first_state_from_login(client, "handoff-code")
    _mock_monday_oauth(monkeypatch, email=None)

    response = _complete_monday_oauth(client, state)

    assert response.status_code == 307
    assert response.headers["location"] == "https://app.example.test/monday-handoff/handoff-code"
    assert client.cookies.get(settings.app_session_cookie_name)
    assert client.cookies.get(settings.app_csrf_cookie_name)

    app_user = db_session.query(AppUser).one()
    assert app_user.monday_account_id == "acct"
    assert app_user.monday_user_id == "monday-user"
    assert app_user.monday_email is None

    link = db_session.query(UserMondayLink).one()
    assert link.app_user_id == app_user.id
    assert link.monday_email is None
    assert link.access_token == "monday-token"

    session = db_session.query(AppSession).one()
    assert session.app_user_id == app_user.id
    assert session.revoked_at is None


def test_monday_first_oauth_rejects_handoff_identity_mismatch(client, db_session, monkeypatch):
    _add_handoff_code(db_session)
    state = _monday_first_state_from_login(client, "handoff-code")
    _mock_monday_oauth(monkeypatch, user_id="other-user")

    response = _complete_monday_oauth(client, state)

    assert response.status_code == 403
    assert db_session.query(AppUser).count() == 0
    assert db_session.query(AppSession).count() == 0


@pytest.mark.parametrize("stage", ["token", "me"])
@pytest.mark.parametrize("failure,status", [
    ("timeout", 504), ("connection", 502), ("http", 502),
    ("non_json", 502), ("null", 502), ("list", 502),
])
def test_oauth_upstream_failures_are_handled_without_creating_session(
    client, db_session, monkeypatch, caplog, stage, failure, status,
):
    _add_handoff_code(db_session)
    state = _monday_first_state_from_login(client, "handoff-code")
    calls = []

    class InvalidJsonResponse(FakeResponse):
        def json(self):
            raise ValueError("private-upstream-body")

    def fake_post(url, **kwargs):
        calls.append(url)
        if stage == "me" and url == MONDAY_TOKEN_URL:
            return FakeResponse({"access_token": "private-access-token"})
        if failure == "timeout":
            raise requests.exceptions.ReadTimeout("private-upstream-body")
        if failure == "connection":
            raise requests.exceptions.ConnectionError("private-upstream-body")
        if failure == "http":
            return FakeResponse({"error": "private-upstream-body"}, ok=False, status_code=503)
        if failure == "non_json":
            return InvalidJsonResponse({})
        return FakeResponse(None if failure == "null" else [])

    monkeypatch.setattr(monday_auth.requests, "post", fake_post)
    response = _complete_monday_oauth(client, state)

    assert response.status_code == status
    assert "monday" in response.json()["detail"].lower()
    assert calls == ([MONDAY_TOKEN_URL] if stage == "token" else [MONDAY_TOKEN_URL, MONDAY_API_URL])
    assert db_session.query(AppUser).count() == 0
    assert db_session.query(UserMondayLink).count() == 0
    assert db_session.query(AppSession).count() == 0
    assert "set-cookie" not in response.headers
    assert db_session.get(HandoffCode, "handoff-code").used is False
    assert "private-upstream-body" not in response.text + caplog.text
    assert "private-access-token" not in response.text + caplog.text
    assert ("token_exchange" if stage == "token" else "identity_lookup") in caplog.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("payload", [
    {"data": None, "errors": [{"extensions": {"code": "API_TEMPORARILY_BLOCKED"}}]},
    {"data": None},
    {"data": []},
    {"data": {"me": None}},
    {"data": {"me": "invalid"}},
    {"data": {"me": {"id": "monday-user", "account": "invalid"}}},
    {"data": {"me": {"id": "monday-user", "account": {"id": "acct"}}}, "errors": [{"message": "private-upstream-body"}]},
])
def test_oauth_invalid_identity_response_does_not_crash_or_create_session(
    client, db_session, monkeypatch, payload,
):
    _add_handoff_code(db_session)
    state = _monday_first_state_from_login(client, "handoff-code")

    def fake_post(url, **kwargs):
        return FakeResponse({"access_token": "monday-token"} if url == MONDAY_TOKEN_URL else payload)

    monkeypatch.setattr(monday_auth.requests, "post", fake_post)
    response = _complete_monday_oauth(client, state)

    assert response.status_code == 502
    assert db_session.query(AppUser).count() == 0
    assert db_session.query(AppSession).count() == 0
    assert "set-cookie" not in response.headers
    assert "private-upstream-body" not in response.text


@pytest.mark.parametrize("mode", ["monday_first", "connect"])
def test_browser_oauth_timeout_offers_fresh_sign_in_and_can_recover(client, db_session, monkeypatch, mode):
    _add_handoff_code(db_session)
    if mode == "monday_first":
        state = _monday_first_state_from_login(client, "handoff-code")
    else:
        state = monday_auth._build_state({"mode": mode, "sub": str(uuid.uuid4()), "return_to": "/tasks/task-1"})
    calls = []

    def timed_out(url, **kwargs):
        calls.append(url)
        raise requests.exceptions.ReadTimeout("private-upstream-body")

    monkeypatch.setattr(monday_auth.requests, "post", timed_out)
    response = client.get(
        "/auth/monday/callback",
        params={"code": "old-oauth-code", "state": state},
        headers={"Accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert response.status_code == 504
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "Monday sign-in timed out" in response.text
    assert "Try signing in again" in response.text
    assert "old-oauth-code" not in response.text
    assert "private-upstream-body" not in response.text
    assert "set-cookie" not in response.headers
    assert calls == [MONDAY_TOKEN_URL]
    assert db_session.query(AppSession).count() == 0
    retry_url = unescape(re.search(r'href="([^"]+)"', response.text).group(1))
    if mode == "monday_first":
        assert retry_url == "https://app.example.test/monday-handoff/handoff-code"
        # The handoff page starts login again, obtaining a fresh authorization code.
        state = _monday_first_state_from_login(client, "handoff-code")
    else:
        parsed = urlparse(retry_url)
        assert parsed.netloc == "app.example.test"
        assert parsed.path == "/connect-monday"
        assert parse_qs(parsed.query) == {"returnTo": ["https://app.example.test/tasks/task-1"]}

    _mock_monday_oauth(monkeypatch)
    response = client.get(
        "/auth/monday/callback", params={"code": "fresh-oauth-code", "state": state}, follow_redirects=False,
    )
    assert response.status_code == 307
    assert db_session.query(AppUser).count() == 1
    assert db_session.query(UserMondayLink).count() == 1
    assert db_session.query(AppSession).count() == 1


@pytest.mark.parametrize("token_duration,identity_budget", [(1, 10), (11, 10), (17, 6)])
def test_oauth_allows_slower_token_response_with_room_for_identity_lookup(
    client, db_session, monkeypatch, token_duration, identity_budget,
):
    _add_handoff_code(db_session)
    state = _monday_first_state_from_login(client, "handoff-code")
    _mock_monday_oauth(monkeypatch)
    successful_post = monday_auth.requests.post
    timeouts = []
    now = [0.0]
    monkeypatch.setattr(monday_auth, "monotonic", lambda: now[0])

    def slow_token_post(url, **kwargs):
        timeout = kwargs["timeout"]
        timeouts.append(timeout.total)
        # Model an 11-second response without sleeping. The old 10s timeout
        # would fail; the new read allowance accepts it.
        if url == MONDAY_TOKEN_URL and timeout.read_timeout <= 11:
            raise requests.exceptions.ReadTimeout()
        if url == MONDAY_TOKEN_URL:
            now[0] += token_duration
        assert timeout.connect_timeout <= 3
        return successful_post(url, **kwargs)

    monkeypatch.setattr(monday_auth.requests, "post", slow_token_post)
    assert _complete_monday_oauth(client, state).status_code == 307
    assert timeouts[1] == identity_budget
    assert token_duration + timeouts[1] <= 23  # Leave headroom under Netlify's 26-second proxy limit.


def test_oauth_does_not_start_identity_lookup_when_time_budget_is_exhausted(client, db_session, monkeypatch):
    _add_handoff_code(db_session)
    state = _monday_first_state_from_login(client, "handoff-code")
    times = iter([0, 24])
    monkeypatch.setattr(monday_auth, "monotonic", lambda: next(times))
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse({"access_token": "monday-token"})

    monkeypatch.setattr(monday_auth.requests, "post", fake_post)
    response = _complete_monday_oauth(client, state)
    assert response.status_code == 504
    assert calls == [MONDAY_TOKEN_URL]
    assert db_session.query(AppSession).count() == 0


def test_cookie_session_resolves_handoff_and_authorizes_task_chat_and_signed_url(
    client,
    db_session,
    monkeypatch,
):
    _add_handoff_code(db_session)
    state = _monday_first_state_from_login(client, "handoff-code")
    _mock_monday_oauth(monkeypatch)
    callback_response = _complete_monday_oauth(client, state)
    assert callback_response.status_code == 307

    monkeypatch.setattr(monday_handoff, "can_read_item", lambda access_token, item_id: True)
    monkeypatch.setattr(tasks, "can_read_item", lambda access_token, item_id: True)
    monkeypatch.setattr(
        monday_handoff,
        "fetch_desired_source_revision",
        lambda item_id, access_token=None: None,
    )
    monkeypatch.setattr(monday_handoff, "_run_sync_pipeline_background", lambda *args, **kwargs: None)

    resolve_response = client.post(
        "/api/monday/handoff/resolve",
        json={"code": "handoff-code"},
        headers=_csrf_headers(client),
    )
    assert resolve_response.status_code == 200
    assert resolve_response.json() == {"externalTaskKey": "acct:board-1:item-1"}

    summary_response = client.get("/api/tasks/acct:board-1:item-1/summary")
    assert summary_response.status_code == 200

    snapshot = TaskSnapshot(
        id=uuid.uuid4(),
        external_task_key="acct:board-1:item-1",
        snapshot_version="rev-1",
        task_context_json={"id": "item-1"},
    )
    file_record = TaskFile(
        id=uuid.uuid4(),
        external_task_key="acct:board-1:item-1",
        snapshot_id=snapshot.id,
        kind="attachment_pdf",
        original_filename="source.pdf",
        bucket="raw-monday",
        object_path="source.pdf",
    )
    db_session.add_all([snapshot, file_record])
    db_session.commit()

    class FakeBucket:
        def create_signed_url(self, object_path, expires_in):
            assert object_path == "source.pdf"
            return {"signedURL": "https://signed.example/source.pdf"}

    class FakeStorage:
        def from_(self, bucket):
            assert bucket == "raw-monday"
            return FakeBucket()

    class FakeSupabase:
        storage = FakeStorage()

    monkeypatch.setattr(tasks, "supabase", FakeSupabase())

    signed_url_response = client.get(
        f"/api/tasks/acct:board-1:item-1/files/{file_record.id}/signed-url"
    )
    assert signed_url_response.status_code == 200
    assert signed_url_response.json()["url"] == "https://signed.example/source.pdf"

    file_record.storage_status = "unsupported"
    file_record.storage_error_code = "object_too_large"
    file_record.storage_error_detail = "File exceeds the configured storage limit"
    db_session.commit()

    sources_response = client.get("/api/tasks/acct:board-1:item-1/sources")
    assert sources_response.status_code == 200
    assert sources_response.json()["files"] == [
        {
            "id": str(file_record.id),
            "kind": "attachment_pdf",
            "originalFilename": "source.pdf",
            "mimeType": None,
            "sizeBytes": None,
            "mondayAssetId": None,
            "storageStatus": "unsupported",
            "storageErrorCode": "object_too_large",
            "storageErrorDetail": "File exceeds the configured storage limit",
            "downloadAvailable": False,
            "createdAt": file_record.created_at.isoformat().replace("+00:00", "Z"),
        }
    ]

    unavailable_response = client.get(
        f"/api/tasks/acct:board-1:item-1/files/{file_record.id}/signed-url"
    )
    assert unavailable_response.status_code == 409
    assert unavailable_response.json()["detail"]["storageErrorCode"] == "object_too_large"

    monkeypatch.setattr(
        chat,
        "_run_bounded_retrieval",
        lambda **kwargs: ("answer", [], True),
    )
    chat_response = client.post(
        "/api/chat/complete",
        json={"externalTaskKey": "acct:board-1:item-1", "message": "hello"},
        headers=_csrf_headers(client),
    )
    assert chat_response.status_code == 200
    assert chat_response.json()["content"] == "answer"


@pytest.fixture()
def streaming_user(client, db_session, monkeypatch):
    _add_handoff_code(db_session)
    state = _monday_first_state_from_login(client, "handoff-code")
    _mock_monday_oauth(monkeypatch)
    assert _complete_monday_oauth(client, state).status_code == 307
    db_session.add(Task(external_task_key="acct:board-1:item-1", account_id="acct", board_id="board-1", item_id="item-1"))
    db_session.commit()
    monkeypatch.setattr(tasks, "can_read_item", lambda *args: True)
    return {"externalTaskKey": "acct:board-1:item-1", "message": "hello"}


def test_monday_first_session_streams_without_supabase_token(client, streaming_user, monkeypatch):
    async def events(payload):
        yield "status", {"message": "Preparing"}
        yield "delta", {"text": "Hello"}
        yield "done", {"content": "Hello", "citations": [], "ok": True}
    monkeypatch.setattr(chat, "_chat_events", events)
    response = client.post("/api/chat/stream", json=streaming_user, headers=_csrf_headers(client))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "no-store" in response.headers["cache-control"]
    assert "event: delta" in response.text and "event: done" in response.text
    assert "authorization" not in response.request.headers
    assert not any(name.startswith("sb-") for name in client.cookies)


@pytest.mark.parametrize("failure,status", [("missing", 401), ("invalid", 401), ("expired", 401), ("revoked", 401), ("csrf", 403), ("missing_csrf", 403), ("denied", 403), ("account", 403)])
def test_stream_rejects_access_before_generation(client, db_session, streaming_user, monkeypatch, failure, status):
    headers = _csrf_headers(client)
    if failure == "missing":
        client.cookies.clear()
    elif failure == "invalid":
        client.cookies.clear()
        client.cookies.set("daa_session", "invalid")
    elif failure in {"expired", "revoked"}:
        session = db_session.query(AppSession).one()
        if failure == "expired":
            session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        else:
            session.revoked_at = datetime.now(timezone.utc)
        db_session.commit()
    elif failure == "csrf":
        headers = {"X-CSRF-Token": "wrong"}
    elif failure == "missing_csrf":
        headers = {}
    elif failure == "denied":
        monkeypatch.setattr(tasks, "can_read_item", lambda *args: False)
    else:
        db_session.query(UserMondayLink).delete()
        db_session.commit()
    monkeypatch.setattr(chat, "_chat_events", lambda *args: pytest.fail("Denied requests must not generate"))
    response = client.post("/api/chat/stream", json=streaming_user, headers=headers)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    assert "event:" not in response.text


def test_probe_is_disabled_by_default_and_checks_access_when_enabled(client, streaming_user, monkeypatch):
    monkeypatch.setattr(chat.settings, "chat_stream_probe_enabled", False)
    payload = {"externalTaskKey": streaming_user["externalTaskKey"], "durationSeconds": 1}
    assert client.post("/api/chat/stream/probe", json=payload, headers=_csrf_headers(client)).status_code == 404
    monkeypatch.setattr(chat.settings, "chat_stream_probe_enabled", True)
    monkeypatch.setattr(tasks, "can_read_item", lambda *args: False)
    assert client.post("/api/chat/stream/probe", json=payload, headers=_csrf_headers(client)).status_code == 403
    monkeypatch.setattr(tasks, "can_read_item", lambda *args: True)
    response = client.post("/api/chat/stream/probe", json=payload, headers=_csrf_headers(client))
    assert response.status_code == 200
    assert "event: done" in response.text
    assert "Transport probe completed" in response.text
