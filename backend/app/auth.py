from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Optional

import jwt
from jwt import PyJWKClient
from fastapi import Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import AppSession

_jwks_client = PyJWKClient(settings.supabase_jwks_url)

@dataclass
class CurrentUser:
    id: str
    source: str = "session"
    session_id: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _decode_supabase_user_id(authorization: Optional[str]) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None

    token = authorization.split(" ", 1)[1]
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token).key
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=["ES256"],
            options={"verify_aud": False},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = payload.get("sub") or payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    return str(user_id)


def _session_from_cookie(db: Session, session_token: str) -> AppSession | None:
    session = (
        db.query(AppSession)
        .filter_by(session_token_hash=_hash_session_token(session_token))
        .one_or_none()
    )
    if session is None or session.revoked_at is not None:
        return None
    if _as_aware_utc(session.expires_at) <= _utcnow():
        return None
    return session


def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> CurrentUser:
    session_token = request.cookies.get(settings.app_session_cookie_name)
    if session_token:
        session = _session_from_cookie(db, session_token)
        if session is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
        session.last_seen_at = _utcnow()
        db.commit()
        return CurrentUser(id=session.app_user_id, source="session", session_id=session.id)

    bearer_user_id = _decode_supabase_user_id(authorization)
    if bearer_user_id:
        return CurrentUser(id=bearer_user_id, source="supabase")

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing app session")


def create_app_session(
    db: Session,
    response: Response,
    *,
    app_user_id: str,
    request: Request | None = None,
) -> str:
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = _utcnow() + timedelta(seconds=settings.app_session_max_age_seconds)

    session = AppSession(
        app_user_id=app_user_id,
        session_token_hash=_hash_session_token(session_token),
        csrf_token=csrf_token,
        expires_at=expires_at,
        user_agent=request.headers.get("user-agent") if request is not None else None,
        ip_address=request.client.host if request is not None and request.client else None,
    )
    db.add(session)

    cookie_kwargs = {
        "secure": settings.app_session_cookie_secure,
        "samesite": settings.app_session_cookie_samesite,
        "domain": settings.app_session_cookie_domain,
        "path": "/",
        "max_age": settings.app_session_max_age_seconds,
    }
    response.set_cookie(
        settings.app_session_cookie_name,
        session_token,
        httponly=True,
        **cookie_kwargs,
    )
    response.set_cookie(
        settings.app_csrf_cookie_name,
        csrf_token,
        httponly=False,
        **cookie_kwargs,
    )
    return session_token


def clear_app_session_cookies(response: Response) -> None:
    cookie_kwargs = {
        "secure": settings.app_session_cookie_secure,
        "samesite": settings.app_session_cookie_samesite,
        "domain": settings.app_session_cookie_domain,
        "path": "/",
    }
    response.delete_cookie(settings.app_session_cookie_name, httponly=True, **cookie_kwargs)
    response.delete_cookie(settings.app_csrf_cookie_name, httponly=False, **cookie_kwargs)


def revoke_current_session(request: Request, db: Session) -> None:
    session_token = request.cookies.get(settings.app_session_cookie_name)
    if not session_token:
        return

    session = (
        db.query(AppSession)
        .filter_by(session_token_hash=_hash_session_token(session_token))
        .one_or_none()
    )
    if session is not None and session.revoked_at is None:
        session.revoked_at = _utcnow()
        db.commit()


def require_csrf_token(request: Request, authorization: Optional[str] = Header(None)) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return
    session_token = request.cookies.get(settings.app_session_cookie_name)
    if not session_token:
        return
    if authorization and authorization.lower().startswith("bearer "):
        return

    csrf_cookie = request.cookies.get(settings.app_csrf_cookie_name)
    csrf_header = request.headers.get("x-csrf-token")
    if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")