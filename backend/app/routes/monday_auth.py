from datetime import datetime, timedelta, timezone
from html import escape
import logging
import secrets
from time import monotonic
from urllib.parse import quote, urlencode
from urllib.parse import urlparse

import jwt
import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from urllib3.util import Timeout

from ..auth import (
    CurrentUser,
    clear_app_session_cookies,
    create_app_session,
    get_current_user,
    require_csrf_token,
    revoke_current_session,
)
from ..config import settings
from ..db import get_db
from ..models import AppUser, HandoffCode, UserMondayLink
from ..monday_client import MONDAY_API_URL, MONDAY_OAUTH_URL, MONDAY_TOKEN_URL, monday_headers

router = APIRouter(prefix="/auth/monday", tags=["monday-auth"])
logger = logging.getLogger(__name__)

def _redirect_uri() -> str:
    if settings.monday_oauth_redirect_uri:
        return settings.monday_oauth_redirect_uri
    return f"{settings.backend_base_url.rstrip('/')}/auth/monday/callback"

def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _main_app_url(path: str = "/") -> str:
    return f"{settings.main_app_base_url.rstrip('/')}{path}"


def _safe_return_to(return_to: str | None, default_path: str) -> str:
    default_url = _main_app_url(default_path)
    if not return_to:
        return default_url

    parsed = urlparse(return_to)
    if not parsed.netloc and return_to.startswith("/"):
        return _main_app_url(return_to)

    main_app = urlparse(settings.main_app_base_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc == main_app.netloc:
        return return_to

    return default_url


def _validate_handoff_code(db: Session, code: str) -> HandoffCode:
    handoff_code = db.get(HandoffCode, code)
    now = datetime.now(timezone.utc)
    if not handoff_code or handoff_code.used or _as_aware_utc(handoff_code.expires_at) <= now:
        raise HTTPException(status_code=400, detail="Invalid or expired handoff code")
    return handoff_code


def _build_state(payload: dict) -> str:
    payload = {
        **payload,
        "nonce": secrets.token_urlsafe(8),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    return jwt.encode(payload, settings.monday_signing_secret, algorithm="HS256")


def _parse_state(state: str) -> dict:
    try:
        payload = jwt.decode(
            state,
            settings.monday_signing_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=400, detail="Invalid state")
    mode = payload.get("mode")
    if mode not in {"connect", "monday_first"}:
        raise HTTPException(status_code=400, detail="Invalid state payload")
    return payload


def _oauth_url(state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.monday_client_id,
            "redirect_uri": _redirect_uri(),
            "state": state,
        }
    )
    return f"{MONDAY_OAUTH_URL}?{query}"


def _oauth_post(url: str, *, stage: str, timeout: Timeout, **kwargs) -> dict:
    # Do not replay the authorization-code exchange after a read timeout:
    # Monday may have consumed the code before the response was lost.
    try:
        response = requests.post(url, timeout=timeout, **kwargs)
    except requests.exceptions.Timeout:
        logger.warning("monday OAuth %s timed out", stage)
        raise HTTPException(status_code=504, detail="Monday sign-in timed out. Please start sign-in again.") from None
    except requests.exceptions.RequestException:
        logger.warning("monday OAuth %s connection failed", stage)
        raise HTTPException(status_code=502, detail="Could not reach Monday. Please start sign-in again.") from None

    if not response.ok:
        logger.warning("monday OAuth %s failed upstream_status=%s", stage, response.status_code)
        raise HTTPException(status_code=502, detail="Monday could not complete sign-in. Please start sign-in again.")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or payload.get("errors") or payload.get("error_code") or payload.get("error"):
        # Raw bodies/exceptions can contain tokens or other private data.
        logger.warning("monday OAuth %s returned an invalid or error response", stage)
        raise HTTPException(status_code=502, detail="Monday returned an invalid sign-in response. Please start sign-in again.")
    return payload


def _exchange_monday_code(code: str) -> dict:
    token_data = _oauth_post(
        MONDAY_TOKEN_URL,
        stage="token_exchange",
        data={
            "client_id": settings.monday_client_id,
            "client_secret": settings.monday_client_secret,
            "code": code,
            "redirect_uri": _redirect_uri(),
        },
        # Netlify's proxy limit is 26s. Allow a slower token response while
        # reserving time for the identity lookup and session persistence.
        timeout=Timeout(total=18, connect=3, read=15),
    )
    if not isinstance(token_data.get("access_token"), str) or not token_data["access_token"].strip():
        logger.warning("monday OAuth token_exchange returned no access token")
        raise HTTPException(status_code=502, detail="Monday did not return an access token. Please start sign-in again.")
    return token_data


def _monday_me(access_token: str, *, timeout_seconds: float = 10) -> dict:
    if timeout_seconds <= 0:
        logger.warning("monday OAuth identity_lookup skipped: request budget exhausted")
        raise HTTPException(status_code=504, detail="Monday sign-in timed out. Please start sign-in again.")
    payload = _oauth_post(
        MONDAY_API_URL,
        stage="identity_lookup",
        json={"query": "query { me { id name email account { id } } }"},
        headers=monday_headers(access_token),
        timeout=Timeout(total=timeout_seconds, connect=min(3, timeout_seconds)),
    )
    data = payload.get("data")
    me = data.get("me") if isinstance(data, dict) else None
    account = me.get("account") if isinstance(me, dict) else None
    monday_user_id = me.get("id") if isinstance(me, dict) else None
    monday_account_id = account.get("id") if isinstance(account, dict) else None
    if not monday_user_id or not monday_account_id:
        logger.warning("monday OAuth identity_lookup returned no user/account identity")
        raise HTTPException(status_code=502, detail="monday me query missing id/account")
    return me


def _oauth_failure_response(request: Request, state_payload: dict, exc: HTTPException):
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

    if state_payload["mode"] == "monday_first":
        handoff_code = quote(str(state_payload.get("handoff_code") or ""), safe="")
        retry_url = _main_app_url(f"/monday-handoff/{handoff_code}")
    else:
        retry_url = _main_app_url("/connect-monday?") + urlencode({
            "returnTo": _safe_return_to(state_payload.get("return_to"), "/?monday=connected"),
        })
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Monday sign-in interrupted</title></head>"
        "<body><main style=\"font-family:system-ui;max-width:32rem;margin:10vh auto;padding:1.5rem\">"
        "<h1>Monday sign-in interrupted</h1>"
        f"<p>{escape(str(exc.detail))}</p>"
        f"<p><a href=\"{escape(retry_url, quote=True)}\">Try signing in again</a></p>"
        "<p>If your sign-in link has expired, reopen the item from Monday.</p>"
        "</main></body></html>",
        status_code=exc.status_code,
        headers=headers,
    )


def _ensure_app_user(
    db: Session,
    *,
    app_user_id: str | None = None,
    monday_account_id: str | None = None,
    monday_user_id: str | None = None,
    monday_email: str | None = None,
    monday_user_name: str | None = None,
    auth_provider: str = "monday",
) -> AppUser:
    app_user = db.get(AppUser, app_user_id) if app_user_id else None
    if app_user is None:
        app_user = AppUser(
            id=app_user_id,
            auth_provider=auth_provider,
            monday_account_id=monday_account_id,
            monday_user_id=monday_user_id,
            monday_email=monday_email,
            monday_user_name=monday_user_name,
        )
        db.add(app_user)
        db.flush()
        return app_user

    app_user.monday_account_id = monday_account_id or app_user.monday_account_id
    app_user.monday_user_id = monday_user_id or app_user.monday_user_id
    app_user.monday_email = monday_email
    app_user.monday_user_name = monday_user_name
    return app_user


def _upsert_monday_link(
    db: Session,
    *,
    app_user: AppUser,
    monday_account_id: str,
    monday_user_id: str,
    monday_email: str | None,
    monday_user_name: str | None,
    access_token: str,
    refresh_token: str | None,
    expires_in: int | None,
) -> UserMondayLink:
    link = (
        db.query(UserMondayLink)
        .filter_by(
            monday_account_id=monday_account_id,
            monday_user_id=monday_user_id,
        )
        .one_or_none()
    )
    if link is None:
        link = UserMondayLink(
            app_user_id=app_user.id,
            target_user_id=app_user.id,
            monday_account_id=monday_account_id,
            monday_user_id=monday_user_id,
            access_token=access_token,
        )
        db.add(link)
    elif link.app_user_id != app_user.id:
        raise HTTPException(status_code=409, detail="Monday identity already linked")

    link.access_token = access_token
    link.monday_email = monday_email
    link.monday_user_name = monday_user_name
    link.app_user_id = app_user.id
    if refresh_token:
        link.refresh_token = refresh_token
    if expires_in:
        link.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    return link


def _app_user_for_monday_identity(
    db: Session,
    *,
    monday_account_id: str,
    monday_user_id: str,
    monday_email: str | None,
    monday_user_name: str | None,
) -> AppUser:
    link = (
        db.query(UserMondayLink)
        .filter_by(monday_account_id=monday_account_id, monday_user_id=monday_user_id)
        .one_or_none()
    )
    if link is not None:
        return _ensure_app_user(
            db,
            app_user_id=link.app_user_id,
            monday_account_id=monday_account_id,
            monday_user_id=monday_user_id,
            monday_email=monday_email,
            monday_user_name=monday_user_name,
        )

    return _ensure_app_user(
        db,
        monday_account_id=monday_account_id,
        monday_user_id=monday_user_id,
        monday_email=monday_email,
        monday_user_name=monday_user_name,
    )


@router.get("/login")
def monday_login(
    request: Request,
    handoff_code: str | None = None,
    return_to: str | None = None,
    mode: str | None = None,
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
):
    if mode == "monday_first" or handoff_code:
        if not handoff_code:
            raise HTTPException(status_code=400, detail="Missing handoff_code")
        _validate_handoff_code(db, handoff_code)
        state = _build_state(
            {
                "mode": "monday_first",
                "handoff_code": handoff_code,
                "return_to": return_to or f"/monday-handoff/{handoff_code}",
            }
        )
        return RedirectResponse(_oauth_url(state))

    current_user: CurrentUser = get_current_user(request, authorization, db)
    state = _build_state(
        {
            "mode": "connect",
            "sub": current_user.id,
            "return_to": return_to or "/?monday=connected",
        }
    )
    url = _oauth_url(state)
    return JSONResponse({"url": url})

@router.get("/callback")
def monday_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")

    state_payload = _parse_state(state)

    upstream_deadline = monotonic() + 23
    try:
        token_data = _exchange_monday_code(code)
        access_token = token_data["access_token"]
        me = _monday_me(access_token, timeout_seconds=min(10, upstream_deadline - monotonic()))
    except HTTPException as exc:
        return _oauth_failure_response(request, state_payload, exc)
    monday_user_id = str(me["id"])
    monday_account_id = str((me["account"] or {})["id"])
    monday_email = me.get("email")
    monday_user_name = me.get("name")

    if state_payload["mode"] == "monday_first":
        handoff_code = _validate_handoff_code(db, str(state_payload.get("handoff_code") or ""))
        if (
            str(handoff_code.monday_account_id) != monday_account_id
            or str(handoff_code.monday_user_id) != monday_user_id
        ):
            raise HTTPException(status_code=403, detail="Monday OAuth identity does not match handoff")
        app_user = _app_user_for_monday_identity(
            db,
            monday_account_id=monday_account_id,
            monday_user_id=monday_user_id,
            monday_email=monday_email,
            monday_user_name=monday_user_name,
        )
        return_to = _safe_return_to(
            state_payload.get("return_to"),
            f"/monday-handoff/{handoff_code.code}",
        )
    else:
        state_user_id = str(state_payload.get("sub") or "")
        if not state_user_id:
            raise HTTPException(status_code=400, detail="Invalid state payload")
        app_user = _ensure_app_user(
            db,
            app_user_id=state_user_id,
            monday_account_id=monday_account_id,
            monday_user_id=monday_user_id,
            monday_email=monday_email,
            monday_user_name=monday_user_name,
            auth_provider="supabase",
        )
        return_to = _safe_return_to(state_payload.get("return_to"), "/?monday=connected")

    _upsert_monday_link(
        db,
        app_user=app_user,
        monday_account_id=monday_account_id,
        monday_user_id=monday_user_id,
        monday_email=monday_email,
        monday_user_name=monday_user_name,
        access_token=access_token,
        refresh_token=token_data.get("refresh_token"),
        expires_in=token_data.get("expires_in"),
    )

    response = RedirectResponse(return_to)
    create_app_session(db, response, app_user_id=app_user.id, request=request)
    db.commit()
    return response


@router.post("/logout", dependencies=[Depends(require_csrf_token)])
def logout(request: Request, db: Session = Depends(get_db)):
    response = JSONResponse({"ok": True})
    revoke_current_session(request, db)
    clear_app_session_cookies(response)
    return response
