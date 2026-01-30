from datetime import datetime, timedelta, timezone
import secrets
from urllib.parse import urlencode

import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..config import settings
from ..db import get_db
from ..models import UserMondayLink
from ..monday_client import MONDAY_API_URL, MONDAY_OAUTH_URL, MONDAY_TOKEN_URL

router = APIRouter(prefix="/auth/monday", tags=["monday-auth"])

def _redirect_uri() -> str:
    if settings.monday_oauth_redirect_uri:
        return settings.monday_oauth_redirect_uri
    return f"{settings.backend_base_url.rstrip('/')}/auth/monday/callback"

def _build_state(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "nonce": secrets.token_urlsafe(8),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    return jwt.encode(payload, settings.monday_signing_secret, algorithm="HS256")

def _parse_state(state: str) -> str:
    try:
        payload = jwt.decode(
            state,
            settings.monday_signing_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=400, detail="Invalid state")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid state payload")
    return str(user_id)

@router.get("/login")
def monday_login(current_user: CurrentUser = Depends(get_current_user)):
    state = _build_state(current_user.id)
    query = urlencode(
        {
            "client_id": settings.monday_client_id,
            "redirect_uri": _redirect_uri(),
            "state": state,
        }
    )
    url = f"{MONDAY_OAUTH_URL}?{query}"
    return JSONResponse({"url": url})

@router.get("/callback")
def monday_callback(
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")

    user_id = _parse_state(state)

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

    me_resp = requests.post(
        MONDAY_API_URL,
        json={"query": "query { me { id account { id } } }"},
        headers={"Authorization": access_token},
        timeout=10,
    )
    if not me_resp.ok:
        raise HTTPException(status_code=502, detail="monday me query failed")

    me = me_resp.json().get("data", {}).get("me") or {}
    monday_user_id = me.get("id")
    monday_account_id = (me.get("account") or {}).get("id")
    if not monday_user_id or not monday_account_id:
        raise HTTPException(status_code=502, detail="monday me query missing id/account")

    link = (
        db.query(UserMondayLink)
        .filter_by(
            target_user_id=user_id,
            monday_user_id=str(monday_user_id),
            monday_account_id=str(monday_account_id),
        )
        .one_or_none()
    )
    if link is None:
        link = UserMondayLink(
            target_user_id=user_id,
            monday_user_id=str(monday_user_id),
            monday_account_id=str(monday_account_id),
            access_token=access_token,
        )
        db.add(link)
    else:
        link.access_token = access_token

    expires_in = token_data.get("expires_in")
    if expires_in:
        link.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    refresh_token = token_data.get("refresh_token")
    if refresh_token:
        link.refresh_token = refresh_token

    db.commit()
    return RedirectResponse(f"{settings.main_app_base_url.rstrip('/')}/?monday=connected")