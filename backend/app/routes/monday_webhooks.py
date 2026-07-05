from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Any, Iterable, Optional
import uuid

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import MondayWebhookEvent
from ..monday_client import fetch_current_source_revision_inputs
from ..services.auto_sync import (
    QueueResult,
    apply_auto_sync_policy_for_item,
    compute_desired_source_revision,
    get_monday_ingestion_access_token,
    utc_now,
)
from ..services.auto_sync_policy import policy_from_settings

router = APIRouter(prefix="/api/monday/webhooks", tags=["monday"])
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizedWebhookEvent:
    idempotency_key: str
    monday_event_id: Optional[str]
    subscription_id: Optional[str]
    trigger_uuid: Optional[str]
    board_id: Optional[str]
    item_id: Optional[str]
    group_id: Optional[str]
    event_type: Optional[str]
    column_id: Optional[str]


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _first_string(source: dict[str, Any], names: Iterable[str]) -> Optional[str]:
    for name in names:
        value = _as_string(source.get(name))
        if value is not None:
            return value
    return None


def _event_body(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event")
    if isinstance(event, dict):
        return event
    return payload


def normalize_webhook_payload(payload: dict[str, Any]) -> NormalizedWebhookEvent:
    event = _event_body(payload)
    board_id = _first_string(event, ("boardId", "board_id", "pulseBoardId", "pulse_board_id"))
    item_id = _first_string(event, ("itemId", "item_id", "pulseId", "pulse_id"))
    trigger_uuid = _first_string(event, ("triggerUuid", "trigger_uuid"))
    monday_event_id = _first_string(event, ("eventId", "event_id", "id"))
    subscription_id = _first_string(
        event,
        ("subscriptionId", "subscription_id", "webhookId", "webhook_id", "appWebhookId", "app_webhook_id"),
    )
    event_type = _first_string(event, ("type", "eventType", "event_type", "event"))
    column_id = _first_string(event, ("columnId", "column_id"))
    group_id = _first_string(
        event,
        (
            "groupId",
            "group_id",
            "destGroupId",
            "dest_group_id",
            "destinationGroupId",
            "destination_group_id",
            "newGroupId",
            "new_group_id",
        ),
    )

    if trigger_uuid:
        idempotency_key = f"trigger:{trigger_uuid}"
    elif monday_event_id and subscription_id:
        idempotency_key = f"event:{subscription_id}:{monday_event_id}"
    elif board_id and item_id and event_type:
        idempotency_key = f"payload:{board_id}:{item_id}:{event_type}:{_json_hash(payload)}"
    else:
        idempotency_key = f"payload:{_json_hash(payload)}"

    return NormalizedWebhookEvent(
        idempotency_key=idempotency_key,
        monday_event_id=monday_event_id,
        subscription_id=subscription_id,
        trigger_uuid=trigger_uuid,
        board_id=board_id,
        item_id=item_id,
        group_id=group_id,
        event_type=event_type,
        column_id=column_id,
    )


def _extract_authorization_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing monday webhook authorization")
    scheme, _, token = authorization.partition(" ")
    if token and scheme.lower() == "bearer":
        return token.strip()
    return authorization.strip()


def _allowed_audiences(request: Request) -> set[str]:
    backend_base_url = settings.backend_base_url.rstrip("/")
    webhook_url = f"{backend_base_url}/api/monday/webhooks"
    return {backend_base_url, webhook_url, str(request.url).rstrip("/")}


def _verify_audience_if_present(token_payload: dict[str, Any], request: Request) -> None:
    audience = token_payload.get("aud")
    if audience is None:
        return
    audiences = audience if isinstance(audience, list) else [audience]
    normalized = {_as_string(value) for value in audiences}
    if not normalized.intersection(_allowed_audiences(request)):
        raise HTTPException(status_code=401, detail="Invalid monday webhook audience")


def verify_webhook_authorization(authorization: Optional[str], request: Request) -> dict[str, Any]:
    if not settings.monday_signing_secret:
        raise HTTPException(status_code=503, detail="MONDAY_SIGNING_SECRET is not configured")
    token = _extract_authorization_token(authorization)
    try:
        token_payload = jwt.decode(
            token,
            settings.monday_signing_secret,
            algorithms=["HS256"],
            options={"require": ["exp"], "verify_aud": False},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Expired monday webhook token")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid monday webhook token")
    _verify_audience_if_present(token_payload, request)
    return token_payload


def _request_is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _is_local_request(request: Request) -> bool:
    host = request.url.hostname or ""
    return host in {"testserver", "localhost", "127.0.0.1", "::1"}


def require_https_for_deployed_webhooks(request: Request) -> None:
    if settings.backend_base_url.lower().startswith("https://") and not _is_local_request(request):
        if not _request_is_https(request):
            raise HTTPException(status_code=400, detail="monday webhooks must use HTTPS")


def _create_event_record(
    db: Session,
    payload: dict[str, Any],
    normalized: NormalizedWebhookEvent,
) -> MondayWebhookEvent:
    event = MondayWebhookEvent(
        id=uuid.uuid4(),
        idempotency_key=normalized.idempotency_key,
        monday_event_id=normalized.monday_event_id,
        subscription_id=normalized.subscription_id,
        trigger_uuid=normalized.trigger_uuid,
        board_id=normalized.board_id,
        item_id=normalized.item_id,
        group_id=normalized.group_id,
        event_type=normalized.event_type,
        column_id=normalized.column_id,
        payload_json=payload,
        received_at=utc_now(),
        authenticated=True,
        status="received",
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _mark_event(
    db: Session,
    event: MondayWebhookEvent,
    *,
    status: str,
    error: Optional[str] = None,
) -> None:
    event.status = status
    event.error = error
    event.processed_at = utc_now()
    db.commit()


def _status_from_queue_result(result: QueueResult) -> str:
    if result.job is not None:
        return "queued"
    if result.decision.lifecycle_state == "completed_retained" and result.task is not None:
        return "retained"
    if result.decision.lifecycle_state == "excluded" and result.task is not None:
        return "cancelled"
    if result.decision.should_queue_sync and result.task is not None:
        return "skipped"
    return "ignored"


def _event_response(
    event: MondayWebhookEvent,
    *,
    status: str,
    result: Optional[QueueResult] = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "status": status,
        "eventId": str(event.id),
        "idempotencyKey": event.idempotency_key,
    }
    if result is not None:
        response["decision"] = result.decision.reason
        response["externalTaskKey"] = result.task.external_task_key if result.task is not None else None
        response["jobId"] = str(result.job.id) if result.job is not None else None
    return response


@router.post("", include_in_schema=True)
@router.post("/", include_in_schema=False)
def monday_webhook(
    payload: dict[str, Any],
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    challenge = payload.get("challenge")
    if challenge is not None:
        return {"challenge": challenge}

    require_https_for_deployed_webhooks(request)
    verify_webhook_authorization(authorization, request)

    normalized = normalize_webhook_payload(payload)
    existing = db.query(MondayWebhookEvent).filter_by(idempotency_key=normalized.idempotency_key).one_or_none()
    if existing is not None:
        return _event_response(existing, status="duplicate")

    event = _create_event_record(db, payload, normalized)
    policy = policy_from_settings()

    if normalized.board_id != policy.board_id:
        _mark_event(db, event, status="ignored", error="board_not_managed")
        return _event_response(event, status="ignored")
    if not normalized.item_id:
        _mark_event(db, event, status="ignored", error="missing_item_id")
        return _event_response(event, status="ignored")

    try:
        access_token = get_monday_ingestion_access_token()
        item = fetch_current_source_revision_inputs(access_token, normalized.item_id)
        desired_source_revision = compute_desired_source_revision(item)
        result = apply_auto_sync_policy_for_item(
            db,
            item,
            trigger_type="webhook",
            desired_source_revision=desired_source_revision,
            policy=policy,
        )
        status = _status_from_queue_result(result)
        event.status = status
        event.processed_at = utc_now()
        event.error = None if status != "ignored" else result.decision.reason
        db.commit()
        return _event_response(event, status=status, result=result)
    except Exception as exc:
        db.rollback()
        event = db.get(MondayWebhookEvent, event.id)
        if event is not None:
            _mark_event(db, event, status="failed", error=str(exc))
        logger.exception("Failed to process monday webhook event %s", normalized.idempotency_key)
        return _event_response(event, status="failed") if event is not None else {"status": "failed"}