from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
import uuid

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import AppSession, AppUser, HandoffCode, TaskFile, TaskSnapshot, UserMondayLink
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

    monkeypatch.setattr(chat, "_run_with_tools", lambda **kwargs: ("answer", [], True))
    chat_response = client.post(
        "/api/chat",
        json={"externalTaskKey": "acct:board-1:item-1", "message": "hello"},
        headers=_csrf_headers(client),
    )
    assert chat_response.status_code == 200
    assert '"content": "answer"' in chat_response.text