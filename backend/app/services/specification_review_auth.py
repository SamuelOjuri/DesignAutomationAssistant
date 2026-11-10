"""Item-bound, cookie-independent Monday identity for Specification Review V1.

Every request verifies the stored user's live OAuth identity and item access.
The review bearer is intentionally distinct from SDK, webhook and app tokens.
It grants no authority to select an arbitrary account, board or item.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import math
import secrets
from urllib.parse import urlencode

from fastapi import HTTPException
import jwt
from pydantic import Field

from .. import monday_client
from ..config import settings
from ..models import AppUser, UserMondayLink
from .specification_policy import (
    specification_publication_allowed, specification_rollout_exclusion,
    specification_scope_exclusion,
)
from .specification_review_contract import MondayId, ReviewActor, ReviewModel


SESSION_ISSUER = "design-automation-assistant/specification-review"
SESSION_AUDIENCE = "specification-review"
LIVE_ACCESS_QUERY = """
query SpecificationReviewAccess($itemIds: [ID!]) {
  me { id account { id } }
  items(ids: $itemIds) { id name state board { id } group { id title } }
}
"""


class ReviewItemContext(ReviewModel):
    accountId: MondayId
    boardId: MondayId
    itemId: MondayId


class SessionBootstrap(ReviewModel):
    sessionToken: str = Field(min_length=1, max_length=16384)
    context: ReviewItemContext


@dataclass(frozen=True)
class ReviewAccess:
    actor: ReviewActor
    board_id: str
    item_id: str
    account_id: str
    access_token: str = field(repr=False)
    capabilities: dict[str, bool]
    snapshot: dict
    scope_reason: str | None = None


def _error(status, code, message):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _identifier(value):
    if isinstance(value, bool):
        return None
    text = str(value) if value is not None else ""
    return text if text.isascii() and text.isdecimal() and int(text) > 0 else None


def _configured_target(account_id, board_id):
    pinned_account = settings.performance_specification_review_monday_account_id
    if str(board_id) != str(settings.design_processing_board_id) or (
        pinned_account is not None and str(account_id) != str(pinned_account)
    ):
        raise _error(403, "reviewer_forbidden", "This account or board is outside Specification Review scope")


def sdk_identity(session_token):
    """Validate the SDK token type using the app client secret, requiring expiry."""
    try:
        payload = jwt.decode(
            session_token, settings.monday_client_secret, algorithms=["HS256"],
            options={"verify_aud": False, "verify_exp": False, "require": ["exp"]},
        )
        # Monday's SDK reference documents an ISO timestamp expiry; NumericDate
        # JWTs are also supported. Verify the signature before reading either,
        # and require an explicit timezone/finite, unexpired expiry in all cases.
        expiry = payload["exp"]
        if isinstance(expiry, bool):
            raise jwt.InvalidTokenError("Invalid SDK expiry")
        if isinstance(expiry, (int, float)) or (isinstance(expiry, str) and expiry.isascii() and expiry.isdecimal()):
            expiry_timestamp = float(expiry)
        elif isinstance(expiry, str) and "T" in expiry:
            expiry_date = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if expiry_date.tzinfo is None:
                raise jwt.InvalidTokenError("SDK expiry requires a timezone")
            expiry_timestamp = expiry_date.timestamp()
        else:
            raise jwt.InvalidTokenError("Invalid SDK expiry")
        if not math.isfinite(expiry_timestamp) or expiry_timestamp <= datetime.now(timezone.utc).timestamp():
            raise jwt.InvalidTokenError("Expired SDK token")
    except (jwt.PyJWTError, ValueError, OverflowError):
        raise _error(401, "authorization_expired", "Monday identity is invalid or expired; reopen the review") from None
    dat = payload.get("dat")
    if not isinstance(dat, dict):
        dat = {}
    user_id = _identifier(dat.get("user_id") or dat.get("userId") or payload.get("user_id") or payload.get("userId"))
    account_id = _identifier(dat.get("account_id") or dat.get("accountId") or payload.get("account_id") or payload.get("accountId"))
    if not user_id or not account_id:
        raise _error(401, "authorization_expired", "Monday session has no valid user/account identity")
    return account_id, user_id


def live_review_item(access_token, *, account_id, user_id, board_id, item_id):
    """One user-authenticated read proves current identity, readability and board."""
    _configured_target(account_id, board_id)
    try:
        payload = monday_client.monday_graphql_request(
            access_token, LIVE_ACCESS_QUERY, {"itemIds": [str(item_id)]}, timeout=10,
        )
    except HTTPException as exc:
        if exc.status_code in {401, 403, 404}:
            raise _error(403, "reviewer_forbidden", "Monday item access is no longer available") from None
        raise _error(503, "source_unavailable", "Monday access verification is temporarily unavailable") from None
    except Exception:
        # Do not disclose upstream bodies or tokens in a response or log.
        raise _error(503, "source_unavailable", "Monday access verification is temporarily unavailable") from None
    data = payload.get("data") if isinstance(payload, dict) else None
    me = data.get("me") if isinstance(data, dict) else None
    account = me.get("account") if isinstance(me, dict) else None
    if not isinstance(me, dict) or not isinstance(account, dict) or str(me.get("id")) != str(user_id) or str(account.get("id")) != str(account_id):
        raise _error(403, "reviewer_forbidden", "The connected Monday identity does not match this review")
    items = data.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise _error(403, "reviewer_forbidden", "You do not have access to this Monday item")
    item = items[0]
    board = item.get("board")
    if not isinstance(board, dict) or str(item.get("id")) != str(item_id) or str(board.get("id")) != str(board_id):
        raise _error(403, "reviewer_forbidden", "Monday item context does not match the requested board/item")
    return {**item, "account_id": str(account_id)}


def review_capabilities(snapshot):
    board = snapshot.get("board")
    group = snapshot.get("group")
    reason = specification_scope_exclusion(
        board_id=board.get("id") if isinstance(board, dict) else None,
        group_id=group.get("id") if isinstance(group, dict) else None,
        item_state=snapshot.get("state"),
    )
    if reason is None and not settings.performance_specification_review_enabled:
        reason = "review_disabled"
    if reason is None:
        reason = specification_rollout_exclusion(snapshot["id"])
    editable = reason is None
    return {
        "read": True, "edit": editable,
        "approve": editable and specification_publication_allowed(snapshot["id"]),
    }, reason


def _session_key():
    return hmac.new(settings.monday_signing_secret.encode(), b"specification-review-session-v1", hashlib.sha256).digest()


def _link(db, account_id, user_id, app_user_id=None):
    query = db.query(UserMondayLink).filter_by(monday_account_id=str(account_id), monday_user_id=str(user_id))
    link = query.one_or_none()
    if link is None or (app_user_id is not None and link.app_user_id != app_user_id):
        db.rollback()
        return None
    user = db.get(AppUser, link.app_user_id)
    if user is None:
        db.rollback()
        return None
    expires = link.token_expires_at
    if expires is not None:
        expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires
        if expires <= datetime.now(timezone.utc):
            db.rollback()
            return None
    result = (str(link.app_user_id), link.access_token)
    # The caller performs remote checks before opening command transactions.
    db.rollback()
    return result


def bootstrap_review_session(db, payload: SessionBootstrap):
    payload = SessionBootstrap.model_validate(payload)
    account_id, user_id = sdk_identity(payload.sessionToken)
    context = payload.context
    if context.accountId != account_id:
        raise _error(403, "reviewer_forbidden", "Monday session account does not match the item context")
    _configured_target(account_id, context.boardId)
    linked = _link(db, account_id, user_id)
    if linked is None:
        from ..routes.monday_auth import _build_state, _oauth_url
        return_path = "/specification-review?" + urlencode({"boardId": context.boardId, "itemId": context.itemId})
        state = _build_state({
            "mode": "specification_review", "monday_account_id": account_id,
            "monday_user_id": user_id, "board_id": context.boardId, "item_id": context.itemId,
            "return_to": return_path,
        })
        return {"status": "oauth_required", "authorization_url": _oauth_url(state)}
    app_user_id, access_token = linked
    snapshot = live_review_item(access_token, account_id=account_id, user_id=user_id, board_id=context.boardId, item_id=context.itemId)
    capabilities, reason = review_capabilities(snapshot)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.performance_specification_review_session_ttl_seconds)
    token = jwt.encode({
        "iss": SESSION_ISSUER, "aud": SESSION_AUDIENCE, "purpose": "specification_review",
        "sub": app_user_id, "account_id": account_id, "user_id": user_id,
        "board_id": context.boardId, "item_id": context.itemId, "capabilities": capabilities,
        "iat": now, "exp": expires_at, "jti": secrets.token_urlsafe(16),
    }, _session_key(), algorithm="HS256")
    return {
        "status": "ready", "access_token": token, "token_type": "Bearer",
        "expires_at": expires_at.isoformat(), "capabilities": capabilities, "scope_reason": reason,
        "account_id": account_id, "user_id": user_id, "board_id": context.boardId, "item_id": context.itemId,
    }


def authenticate_review(db, authorization, board_id, item_id):
    if not isinstance(authorization, str) or not authorization.lower().startswith("bearer "):
        raise _error(401, "authorization_expired", "A Specification Review session is required")
    try:
        claims = jwt.decode(
            authorization.split(" ", 1)[1], _session_key(), algorithms=["HS256"],
            audience=SESSION_AUDIENCE, issuer=SESSION_ISSUER,
            options={"require": ["exp", "iat", "sub", "account_id", "user_id", "board_id", "item_id", "purpose", "capabilities"]},
        )
    except jwt.PyJWTError:
        raise _error(401, "authorization_expired", "Review session is invalid or expired; reopen the review") from None
    account_id, user_id = _identifier(claims.get("account_id")), _identifier(claims.get("user_id"))
    if claims.get("purpose") != "specification_review" or not account_id or not user_id:
        raise _error(401, "authorization_expired", "Invalid review identity")
    if claims["board_id"] != str(board_id) or claims["item_id"] != str(item_id):
        raise _error(403, "reviewer_forbidden", "The review session belongs to a different item")
    _configured_target(account_id, board_id)
    linked = _link(db, account_id, user_id, claims["sub"])
    if linked is None:
        raise _error(401, "authorization_expired", "Reconnect Monday to continue reviewing this item")
    app_user_id, access_token = linked
    snapshot = live_review_item(access_token, account_id=account_id, user_id=user_id, board_id=board_id, item_id=item_id)
    capabilities, reason = review_capabilities(snapshot)
    # A read-only bearer cannot acquire write capabilities after a configuration
    # or group change. Bootstrap again to obtain newly granted capabilities.
    issued = claims["capabilities"]
    if not isinstance(issued, dict) or any(not isinstance(issued.get(key), bool) for key in ("read", "edit", "approve")):
        raise _error(401, "authorization_expired", "Invalid review capabilities")
    capabilities = {key: bool(value and issued.get(key)) for key, value in capabilities.items()}
    return ReviewAccess(
        actor=ReviewActor(app_user_id=app_user_id, monday_account_id=account_id, monday_user_id=user_id),
        board_id=str(board_id), item_id=str(item_id), account_id=account_id,
        access_token=access_token, capabilities=capabilities, snapshot=snapshot, scope_reason=reason,
    )
