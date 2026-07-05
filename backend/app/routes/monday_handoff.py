from datetime import datetime, timedelta, timezone

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..config import settings
from ..db import get_db
from ..monday_client import can_read_item, verify_session_token
from ..models import HandoffCode, Task, TaskSnapshot, UserMondayLink
from ..services.auto_sync import fetch_desired_source_revision
from ..services.auto_sync_purge import mark_expired_task_restoring, record_meaningful_access
from ..schemas import (
    HandoffInitRequest,
    HandoffInitResponse,
    HandoffResolveRequest,
    HandoffResolveResponse,
)

router = APIRouter(prefix="/api/monday/handoff", tags=["monday"])
logger = logging.getLogger(__name__)

MAIN_APP_BASE_URL = settings.main_app_base_url.rstrip("/")


def _task_has_fresh_completed_snapshot(
    db: Session,
    task: Task,
    *,
    current_source_revision: str | None,
) -> bool:
    if task.sync_status != "completed" or task.auto_sync_state == "expired":
        return False
    if not current_source_revision:
        return False
    if current_source_revision not in {task.last_indexed_source_revision, task.latest_snapshot_version}:
        return False
    return (
        db.query(TaskSnapshot)
        .filter_by(
            external_task_key=task.external_task_key,
            snapshot_version=current_source_revision,
        )
        .first()
        is not None
    )


def _safe_current_source_revision(access_token: str, item_id: str) -> str | None:
    try:
        return fetch_desired_source_revision(item_id, access_token=access_token)
    except Exception:
        logger.exception("Unable to check monday source freshness for item %s", item_id)
        return None


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _run_sync_pipeline_background(
    external_task_key: str,
    access_token: str,
    force: bool = False,
) -> None:
    from ..services.sync_pipeline import run_sync_pipeline_background

    run_sync_pipeline_background(external_task_key, access_token, force)

@router.post("/init", response_model=HandoffInitResponse)
def handoff_init(payload: HandoffInitRequest, db: Session = Depends(get_db)):
    if not payload.sessionToken:
        raise HTTPException(status_code=400, detail="Missing sessionToken")

    context = payload.context
    if not context or not context.accountId or not context.boardId or not context.itemId:
        raise HTTPException(status_code=400, detail="Missing monday item context")

    token_payload = verify_session_token(payload.sessionToken)

    # Monday session tokens nest claims inside "dat"
    dat = token_payload.get("dat") or {}

    token_user_id = (
        dat.get("user_id")
        or dat.get("userId")
        or token_payload.get("userId")
        or token_payload.get("user_id")
    )
    token_account_id = (
        dat.get("account_id")
        or dat.get("accountId")
        or token_payload.get("accountId")
        or token_payload.get("account_id")
    )

    if not token_user_id or not token_account_id:
        raise HTTPException(status_code=400, detail="Invalid sessionToken payload")

    if str(context.accountId) != str(token_account_id):
        raise HTTPException(status_code=400, detail="Account mismatch")

    code = secrets.token_urlsafe(16)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    handoff_code = HandoffCode(
        code=code,
        monday_account_id=str(context.accountId),
        monday_board_id=str(context.boardId),
        monday_item_id=str(context.itemId),
        monday_user_id=str(token_user_id),
        expires_at=expires_at,
        used=False,
    )
    db.add(handoff_code)
    db.commit()

    url = f"{MAIN_APP_BASE_URL}/monday-handoff/{code}"
    return HandoffInitResponse(url=url, code=code)

@router.post("/resolve", response_model=HandoffResolveResponse)
def handoff_resolve(
    payload: HandoffResolveRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    background_tasks: BackgroundTasks = None,
):
    if not payload.code:
        raise HTTPException(status_code=400, detail="Missing handoff code")

    handoff_code = db.get(HandoffCode, payload.code)
    now = datetime.now(timezone.utc)
    if not handoff_code or handoff_code.used or _as_aware_utc(handoff_code.expires_at) <= now:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    link = (
        db.query(UserMondayLink)
        .filter_by(
            target_user_id=current_user.id,
            monday_user_id=handoff_code.monday_user_id,
            monday_account_id=handoff_code.monday_account_id,
        )
        .one_or_none()
    )

    if link is None:
        raise HTTPException(status_code=403, detail="Monday account not connected")

    if not can_read_item(link.access_token, handoff_code.monday_item_id):
        raise HTTPException(status_code=403, detail="No access to monday item")

    handoff_code.used = True

    external_task_key = (
        f"{handoff_code.monday_account_id}:{handoff_code.monday_board_id}:{handoff_code.monday_item_id}"
    )
    task = db.get(Task, external_task_key)
    if task is None:
        task = Task(
            external_task_key=external_task_key,
            account_id=handoff_code.monday_account_id,
            board_id=handoff_code.monday_board_id,
            item_id=handoff_code.monday_item_id,
        )
        db.add(task)

    current_source_revision = _safe_current_source_revision(link.access_token, handoff_code.monday_item_id)
    force_refresh = bool(getattr(payload, "force", False))
    has_fresh_snapshot = _task_has_fresh_completed_snapshot(
        db,
        task,
        current_source_revision=current_source_revision,
    )

    should_queue_sync = (
        force_refresh
        or not has_fresh_snapshot
    ) and task.sync_status not in {"queued", "syncing"}
    if force_refresh or task.auto_sync_state == "expired":
        record_meaningful_access(db, task)
    if task.auto_sync_state == "expired":
        mark_expired_task_restoring(db, task)
    if should_queue_sync:
        task.sync_status = "syncing"
        task.sync_started_at = datetime.now(timezone.utc)
        task.sync_completed_at = None
        task.sync_error = None

    db.commit()

    if should_queue_sync and background_tasks is not None:
        background_tasks.add_task(
            _run_sync_pipeline_background,
            external_task_key,
            link.access_token,
            False,
        )

    return HandoffResolveResponse(externalTaskKey=external_task_key)