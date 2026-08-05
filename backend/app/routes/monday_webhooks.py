from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
from typing import Any, Iterable, Optional
import uuid

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import MondayWebhookDispatch, MondayWebhookEvent
from ..monday_client import TransientMondayAPIError, fetch_current_source_revision_inputs
from ..services.auto_sync import (
    QueueResult,
    apply_auto_sync_policy_for_item,
    compute_desired_source_revision,
    get_monday_ingestion_access_token,
    utc_now,
)
from ..services.auto_sync_policy import policy_from_settings
from ..services.db_retry import is_retryable_auto_sync_error, run_transaction_with_retry
from ..services.design_processing_inputs import (
    EMAIL_COLUMN_ID,
    DesignProcessingInputError,
    parse_design_processing_target,
)
from ..services.design_processing_queue import queue_design_processing_snapshot

router = APIRouter(prefix="/api/monday/webhooks", tags=["monday"])
logger = logging.getLogger(__name__)

WEBHOOK_PROCESSING_LEASE = timedelta(minutes=5)
WEBHOOK_DISPATCH_LEASE = timedelta(minutes=5)
DESIGN_OUTPUT_COLUMN_IDS = frozenset({"file_mkza7y37", "file_mm59rntf"})
DESIGN_CREATE_EVENT_TYPES = frozenset({"create_item", "create_pulse"})
DESIGN_MOVE_EVENT_TYPES = frozenset(
    {
        "change_group",
        "move_item_to_group",
        "move_pulse_into_group",
        "move_pulse_to_group",
    }
)
DESIGN_NAME_EVENT_TYPES = frozenset(
    {"change_name", "change_item_name", "change_pulse_name"}
)
WEBHOOK_DISPATCH_CONSUMERS = ("auto_sync", "design_processing")


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


def _verify_shared_secret(shared_secret_token: Optional[str]) -> bool:
    expected = _as_string(settings.monday_webhook_shared_secret)
    received = _as_string(shared_secret_token)
    if expected is None or received is None:
        return False
    return hmac.compare_digest(received, expected)


def verify_webhook_authorization(
    authorization: Optional[str],
    request: Request,
    shared_secret_token: Optional[str] = None,
) -> dict[str, Any]:
    if _verify_shared_secret(shared_secret_token):
        return {"auth": "shared_secret"}

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


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _event_claim_query(db: Session, *, idempotency_key: str):
    query = db.query(MondayWebhookEvent).filter_by(idempotency_key=idempotency_key)
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query


def _claim_event_record(
    db: Session,
    payload: dict[str, Any],
    normalized: NormalizedWebhookEvent,
) -> tuple[MondayWebhookEvent, bool]:
    now = utc_now()
    event = _event_claim_query(db, idempotency_key=normalized.idempotency_key).one_or_none()
    if event is not None:
        stale_processing = (
            event.status == "processing"
            and (
                event.processing_started_at is None
                or _as_aware_utc(event.processing_started_at) <= now - WEBHOOK_PROCESSING_LEASE
            )
        )
        retryable_terminal = event.status in {"failed", "partial_failed"} and (
            _event_has_retryable_dispatches(db, event_id=event.id, now=now)
        )
        if (
            event.status != "received"
            and not stale_processing
            and not retryable_terminal
        ):
            db.commit()
            return event, False

        event.status = "processing"
        event.processing_started_at = now
        event.processed_at = None
        event.error = None
        event.attempt_count = (event.attempt_count or 0) + 1
        event.payload_json = payload
        _ensure_dispatch_children(db, event.id)
        db.commit()
        db.refresh(event)
        return event, True

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
        received_at=now,
        authenticated=True,
        processing_started_at=now,
        attempt_count=1,
        status="processing",
    )
    try:
        db.add(event)
        _ensure_dispatch_children(db, event.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        return _claim_event_record(db, payload, normalized)
    db.refresh(event)
    return event, True


def _dispatch_result_is_retryable(dispatch: MondayWebhookDispatch) -> bool:
    result = dispatch.result_json
    if isinstance(result, dict) and "retryable" in result:
        return bool(result["retryable"])
    return True


def _event_has_retryable_dispatches(
    db: Session,
    *,
    event_id: uuid.UUID,
    now: datetime,
) -> bool:
    dispatches = db.query(MondayWebhookDispatch).filter_by(
        webhook_event_id=event_id
    ).all()
    if not dispatches:
        return True
    for dispatch in dispatches:
        if dispatch.status == "pending":
            return True
        if dispatch.status == "failed" and _dispatch_result_is_retryable(dispatch):
            return True
        if dispatch.status == "processing" and (
            dispatch.processing_started_at is None
            or _as_aware_utc(dispatch.processing_started_at)
            <= now - WEBHOOK_DISPATCH_LEASE
        ):
            return True
    return False


def _ensure_dispatch_children(db: Session, event_id: uuid.UUID) -> None:
    existing = {
        dispatch.consumer
        for dispatch in db.query(MondayWebhookDispatch).filter_by(
            webhook_event_id=event_id
        )
    }
    now = utc_now()
    for consumer in WEBHOOK_DISPATCH_CONSUMERS:
        if consumer not in existing:
            db.add(
                MondayWebhookDispatch(
                    id=uuid.uuid4(),
                    webhook_event_id=event_id,
                    consumer=consumer,
                    status="pending",
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )


def _dispatch_claim_query(db: Session, dispatch_id: uuid.UUID):
    query = db.query(MondayWebhookDispatch).filter_by(id=dispatch_id)
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query


def _claim_dispatch(
    db: Session,
    dispatch_id: uuid.UUID,
) -> tuple[Optional[MondayWebhookDispatch], bool]:
    now = utc_now()
    dispatch = _dispatch_claim_query(db, dispatch_id).one_or_none()
    if dispatch is None:
        return None, False
    stale_processing = dispatch.status == "processing" and (
        dispatch.processing_started_at is None
        or _as_aware_utc(dispatch.processing_started_at)
        <= now - WEBHOOK_DISPATCH_LEASE
    )
    retryable_failure = (
        dispatch.status == "failed" and _dispatch_result_is_retryable(dispatch)
    )
    if dispatch.status != "pending" and not stale_processing and not retryable_failure:
        db.commit()
        return dispatch, False

    dispatch.status = "processing"
    dispatch.outcome = None
    dispatch.job_id = None
    dispatch.attempt_count = (dispatch.attempt_count or 0) + 1
    dispatch.processing_started_at = now
    dispatch.completed_at = None
    dispatch.error = None
    dispatch.result_json = None
    dispatch.updated_at = now
    db.commit()
    db.refresh(dispatch)
    return dispatch, True


def _complete_dispatch(
    dispatch: MondayWebhookDispatch,
    *,
    outcome: str,
    job_id: Optional[uuid.UUID],
    result_json: dict[str, Any],
    now: datetime,
) -> None:
    dispatch.status = "succeeded"
    dispatch.outcome = outcome
    dispatch.job_id = job_id
    dispatch.completed_at = now
    dispatch.processing_started_at = None
    dispatch.error = None
    dispatch.result_json = result_json
    dispatch.updated_at = now


def _mark_dispatch_failed(
    db: Session,
    dispatch_id: uuid.UUID,
    *,
    error: Exception,
    retryable: bool,
) -> None:
    dispatch = db.get(MondayWebhookDispatch, dispatch_id)
    if dispatch is None:
        return
    now = utc_now()
    dispatch.status = "failed"
    dispatch.outcome = None
    dispatch.completed_at = now
    dispatch.processing_started_at = None
    dispatch.error = str(error)
    dispatch.result_json = {"retryable": retryable}
    dispatch.updated_at = now
    db.commit()


def _auto_sync_outcome(result: QueueResult) -> str:
    if result.job is not None:
        return "queued" if result.created_job else "coalesced"
    if result.decision.lifecycle_state == "excluded":
        return "excluded"
    if result.decision.reason == "auto_sync_disabled":
        return "disabled"
    return "ignored"


def _dispatch_auto_sync(
    db: Session,
    dispatch: MondayWebhookDispatch,
    *,
    item: Optional[dict[str, Any]],
    normalized: NormalizedWebhookEvent,
) -> None:
    policy = policy_from_settings()
    now = utc_now()
    if normalized.board_id != policy.board_id:
        _complete_dispatch(
            dispatch,
            outcome="ignored",
            job_id=None,
            result_json={"reason": "board_not_managed"},
            now=now,
        )
        return
    if not normalized.item_id:
        _complete_dispatch(
            dispatch,
            outcome="ignored",
            job_id=None,
            result_json={"reason": "missing_item_id"},
            now=now,
        )
        return
    if item is None:
        raise RuntimeError("current Monday item snapshot is unavailable")

    desired_source_revision = compute_desired_source_revision(item)
    result = apply_auto_sync_policy_for_item(
        db,
        item,
        trigger_type="webhook",
        desired_source_revision=desired_source_revision,
        policy=policy,
    )
    outcome = _auto_sync_outcome(result)
    _complete_dispatch(
        dispatch,
        outcome=outcome,
        job_id=result.job.id if result.job is not None else None,
        result_json={
            "reason": result.decision.reason,
            "externalTaskKey": (
                result.task.external_task_key if result.task is not None else None
            ),
            "createdJob": result.created_job,
        },
        now=now,
    )


def _normalized_event_type(normalized: NormalizedWebhookEvent) -> str:
    return (normalized.event_type or "").strip().lower()


def _design_trigger_type(normalized: NormalizedWebhookEvent) -> Optional[str]:
    event_type = _normalized_event_type(normalized)
    column_id = (normalized.column_id or "").strip()
    if column_id in DESIGN_OUTPUT_COLUMN_IDS:
        return None
    if event_type in DESIGN_CREATE_EVENT_TYPES:
        return "webhook_create"
    if event_type in DESIGN_MOVE_EVENT_TYPES:
        return "webhook_move"
    if column_id == EMAIL_COLUMN_ID:
        return "webhook_email"
    if column_id == "name" or event_type in DESIGN_NAME_EVENT_TYPES:
        return "webhook_name"
    return None


def _dispatch_design_processing(
    db: Session,
    dispatch: MondayWebhookDispatch,
    *,
    item: Optional[dict[str, Any]],
    normalized: NormalizedWebhookEvent,
) -> None:
    now = utc_now()
    if settings.design_processing_mode == "off":
        _complete_dispatch(
            dispatch,
            outcome="disabled",
            job_id=None,
            result_json={"reason": "design_processing_off"},
            now=now,
        )
        return
    if normalized.board_id != str(settings.design_processing_board_id):
        _complete_dispatch(
            dispatch,
            outcome="excluded",
            job_id=None,
            result_json={"reason": "board_not_managed"},
            now=now,
        )
        return
    if not normalized.item_id:
        _complete_dispatch(
            dispatch,
            outcome="ignored",
            job_id=None,
            result_json={"reason": "missing_item_id"},
            now=now,
        )
        return

    trigger_type = _design_trigger_type(normalized)
    if trigger_type is None:
        _complete_dispatch(
            dispatch,
            outcome="ignored",
            job_id=None,
            result_json={"reason": "event_not_actionable"},
            now=now,
        )
        return
    if item is None:
        raise RuntimeError("current Monday item snapshot is unavailable")

    snapshot = parse_design_processing_target(item)
    result = queue_design_processing_snapshot(
        db,
        snapshot,
        trigger_type=trigger_type,
        mode=settings.design_processing_mode,
        pipeline_version=settings.design_processing_pipeline_version,
        expected_board_id=str(settings.design_processing_board_id),
        expected_group_id=str(settings.design_processing_landing_group_id),
        allowlist_item_ids=settings.design_processing_allowlist_item_ids,
        now=now,
    )
    _complete_dispatch(
        dispatch,
        outcome=result.outcome,
        job_id=result.job.id if result.job is not None else None,
        result_json={
            "reason": result.readiness or result.outcome,
            "createdJob": result.created_job,
        },
        now=now,
    )


def _process_dispatch(
    db: Session,
    dispatch: MondayWebhookDispatch,
    *,
    item: Optional[dict[str, Any]],
    normalized: NormalizedWebhookEvent,
) -> None:
    handler = (
        _dispatch_auto_sync
        if dispatch.consumer == "auto_sync"
        else _dispatch_design_processing
    )

    def process_attempt() -> None:
        dispatch_attempt = db.get(MondayWebhookDispatch, dispatch.id)
        if dispatch_attempt is None:
            raise RuntimeError("Webhook dispatch disappeared during processing")
        handler(
            db,
            dispatch_attempt,
            item=item,
            normalized=normalized,
        )
        db.commit()

    run_transaction_with_retry(
        db,
        process_attempt,
        operation_name=f"monday webhook {dispatch.consumer} dispatch {dispatch.id}",
    )


def _aggregate_parent_event(
    db: Session,
    event_id: uuid.UUID,
) -> MondayWebhookEvent:
    event = db.get(MondayWebhookEvent, event_id)
    if event is None:
        raise RuntimeError("Webhook event disappeared during aggregation")
    dispatches = db.query(MondayWebhookDispatch).filter_by(
        webhook_event_id=event_id
    ).all()
    statuses = {dispatch.status for dispatch in dispatches}
    if statuses <= {"succeeded"} and dispatches:
        status = "completed"
    elif statuses <= {"failed"} and dispatches:
        status = "failed"
    elif "pending" in statuses or "processing" in statuses:
        status = "processing"
    elif "failed" in statuses and "succeeded" in statuses:
        status = "partial_failed"
    else:
        status = "processing"

    now = utc_now()
    event.status = status
    event.processing_started_at = None if status != "processing" else event.processing_started_at
    event.processed_at = now if status != "processing" else None
    failed_consumers = [
        dispatch.consumer for dispatch in dispatches if dispatch.status == "failed"
    ]
    event.error = (
        f"failed consumers: {', '.join(sorted(failed_consumers))}"
        if failed_consumers
        else None
    )
    db.commit()
    db.refresh(event)
    return event


def _dispatches_for_response(
    db: Session,
    event_id: uuid.UUID,
) -> dict[str, dict[str, Any]]:
    return {
        dispatch.consumer: {
            "status": dispatch.status,
            "outcome": dispatch.outcome,
            "jobId": str(dispatch.job_id) if dispatch.job_id is not None else None,
            "error": dispatch.error,
            "result": dispatch.result_json,
        }
        for dispatch in db.query(MondayWebhookDispatch).filter_by(
            webhook_event_id=event_id
        )
    }


def _response_status_from_dispatches(
    event: MondayWebhookEvent,
    dispatches: dict[str, dict[str, Any]],
) -> str:
    if event.status in {"failed", "partial_failed", "processing"}:
        return event.status
    auto_sync = dispatches.get("auto_sync") or {}
    design_processing = dispatches.get("design_processing") or {}
    auto_outcome = auto_sync.get("outcome")
    design_outcome = design_processing.get("outcome")
    auto_detail = auto_sync.get("result") or {}
    if auto_outcome in {"queued", "coalesced"}:
        return "queued"
    if design_outcome in {"queued", "coalesced"}:
        return "queued"
    if auto_detail.get("reason") == "completed_retention_only":
        return "retained"
    if auto_outcome == "excluded":
        return "cancelled"
    return "ignored"


def _event_response(
    event: MondayWebhookEvent,
    *,
    status: str,
    dispatches: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "status": status,
        "eventId": str(event.id),
        "idempotencyKey": event.idempotency_key,
    }
    if dispatches is not None:
        response["dispatches"] = dispatches
        auto_result = dispatches.get("auto_sync") or {}
        response["jobId"] = auto_result.get("jobId")
        auto_detail = auto_result.get("result") or {}
        response["decision"] = auto_detail.get("reason")
        response["externalTaskKey"] = auto_detail.get("externalTaskKey")
    return response


@router.post("", include_in_schema=True)
@router.post("/", include_in_schema=False)
def monday_webhook(
    payload: dict[str, Any],
    request: Request,
    authorization: Optional[str] = Header(default=None),
    shared_secret_token: Optional[str] = Query(default=None, alias="token"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    challenge = payload.get("challenge")
    if challenge is not None:
        return {"challenge": challenge}

    require_https_for_deployed_webhooks(request)
    verify_webhook_authorization(authorization, request, shared_secret_token)

    normalized = normalize_webhook_payload(payload)
    event, claimed = _claim_event_record(db, payload, normalized)
    if not claimed:
        response_status = "in_progress" if event.status == "processing" else "duplicate"
        return _event_response(
            event,
            status=response_status,
            dispatches=_dispatches_for_response(db, event.id),
        )

    dispatches = db.query(MondayWebhookDispatch).filter_by(
        webhook_event_id=event.id
    ).order_by(MondayWebhookDispatch.consumer.asc()).all()
    claimable_dispatches: list[MondayWebhookDispatch] = []
    for candidate in dispatches:
        claimed_dispatch, dispatch_claimed = _claim_dispatch(db, candidate.id)
        if dispatch_claimed and claimed_dispatch is not None:
            claimable_dispatches.append(claimed_dispatch)

    item: Optional[dict[str, Any]] = None
    snapshot_error: Optional[Exception] = None
    if claimable_dispatches and normalized.item_id and normalized.board_id in {
        str(settings.auto_sync_board_id),
        str(settings.design_processing_board_id),
    }:
        try:
            access_token = get_monday_ingestion_access_token()
            item = fetch_current_source_revision_inputs(
                access_token,
                normalized.item_id,
            )
        except Exception as exc:
            snapshot_error = exc

    retryable_failure = False
    for dispatch in claimable_dispatches:
        try:
            design_requires_snapshot = (
                dispatch.consumer == "design_processing"
                and settings.design_processing_mode != "off"
                and normalized.board_id == str(settings.design_processing_board_id)
                and normalized.item_id is not None
                and _design_trigger_type(normalized) is not None
            )
            auto_sync_requires_snapshot = (
                dispatch.consumer == "auto_sync"
                and normalized.board_id == str(settings.auto_sync_board_id)
                and normalized.item_id is not None
            )
            if snapshot_error is not None and (
                design_requires_snapshot or auto_sync_requires_snapshot
            ):
                raise snapshot_error
            _process_dispatch(
                db,
                dispatch,
                item=item,
                normalized=normalized,
            )
        except Exception as exc:
            db.rollback()
            retryable = (
                isinstance(exc, (TransientMondayAPIError, DesignProcessingInputError))
                or is_retryable_auto_sync_error(exc)
            )
            _mark_dispatch_failed(
                db,
                dispatch.id,
                error=exc,
                retryable=retryable,
            )
            retryable_failure = retryable_failure or retryable
            logger.exception(
                "Failed to process monday webhook %s consumer %s",
                normalized.idempotency_key,
                dispatch.consumer,
            )

    event = _aggregate_parent_event(db, event.id)
    response_dispatches = _dispatches_for_response(db, event.id)
    response_status = _response_status_from_dispatches(event, response_dispatches)
    if retryable_failure:
        raise HTTPException(
            status_code=503,
            detail="Webhook processing temporarily unavailable",
            headers={"Retry-After": "1"},
        )
    return _event_response(
        event,
        status=response_status,
        dispatches=response_dispatches,
    )
