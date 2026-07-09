from datetime import datetime, timedelta, timezone
import secrets
from urllib.parse import urlencode
from urllib.parse import urlparse

import jwt
import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

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


def _monday_me(access_token: str) -> dict:
    me_resp = requests.post(
        MONDAY_API_URL,
        json={"query": "query { me { id name email account { id } } }"},
        headers=monday_headers(access_token),
        timeout=10,
    )
    if not me_resp.ok:
        raise HTTPException(status_code=502, detail="monday me query failed")

    me = me_resp.json().get("data", {}).get("me") or {}
    monday_user_id = me.get("id")
    monday_account_id = (me.get("account") or {}).get("id")
    if not monday_user_id or not monday_account_id:
        raise HTTPException(status_code=502, detail="monday me query missing id/account")
    return me


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

    token_resp = requests.post(
        MONDAY_TOKEN_URL,
        data={
            "client_id": settings.monday_client_id,
            "client_secret": settings.monday_client_secret,
            "code": code,
            "redirect_uri": _redirect_uri(),
        },
        timeout=10,
    )
    if not token_resp.ok:
        raise HTTPException(status_code=502, detail="monday token exchange failed")

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="monday token missing access_token")

    me = _monday_me(access_token)
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