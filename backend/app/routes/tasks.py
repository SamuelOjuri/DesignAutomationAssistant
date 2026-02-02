from datetime import datetime, timedelta, timezone
from typing import Optional, Any

from fastapi import APIRouter, Body, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import Task, TaskSnapshot, TaskFile, UserMondayLink
from ..monday_client import can_read_item
from ..services.sync_pipeline import run_sync_pipeline, run_sync_pipeline_background
from ..schemas import (
    TaskSyncRequest,
    TaskSyncResponse,
    TaskSummaryResponse,
    TaskSourcesResponse,
    TaskSourceFile,
    SignedUrlResponse,
)
from ..supabase_client import supabase

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _validate_external_task_key(external_task_key: str) -> None:
    parts = external_task_key.split(":")
    if len(parts) != 3 or any(not part for part in parts):
        raise HTTPException(status_code=400, detail="Invalid externalTaskKey")


def require_task_access(
    external_task_key: str,
    db: Session,
    current_user: CurrentUser,
) -> Task:
    _validate_external_task_key(external_task_key)

    task = db.get(Task, external_task_key)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    link = (
        db.query(UserMondayLink)
        .filter_by(
            target_user_id=current_user.id,
            monday_account_id=task.account_id,
        )
        .one_or_none()
    )
    if link is None:
        raise HTTPException(status_code=403, detail="Monday account not connected")

    if not can_read_item(link.access_token, task.item_id):
        raise HTTPException(status_code=403, detail="No access to monday item")

    return task


def _signed_url_from_response(resp: Any) -> str:
    data = getattr(resp, "data", resp)
    if isinstance(data, dict):
        url = data.get("signedURL") or data.get("signedUrl") or data.get("url")
        if url:
            return url
    raise HTTPException(status_code=502, detail="Failed to create signed URL")


@router.post("/{externalTaskKey}/sync", response_model=TaskSyncResponse)
def sync_task(
    externalTaskKey: str,
    payload: Optional[TaskSyncRequest] = Body(default=None),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    background_tasks: BackgroundTasks = None,
):
    task = require_task_access(externalTaskKey, db, current_user)

    link = (
        db.query(UserMondayLink)
        .filter_by(
            target_user_id=current_user.id,
            monday_account_id=task.account_id,
        )
        .one_or_none()
    )

    if link is None:
        raise HTTPException(status_code=403, detail="Monday account not connected")

    # Check if sync is already in progress
    if task.sync_status == "syncing":
        return TaskSyncResponse(status="already_syncing", snapshotVersion=task.latest_snapshot_version)

    # Mark sync as started immediately
    task.sync_status = "syncing"
    task.sync_started_at = datetime.now(timezone.utc)
    task.sync_error = None
    db.commit()

    # Always run sync in background for immediate response
    # This prevents connection drops during deploys from causing CORS errors
    background_tasks.add_task(
        run_sync_pipeline_background,
        task.external_task_key,
        link.access_token,
        payload.force if payload else False,
    )
    return TaskSyncResponse(status="queued", snapshotVersion=None)


@router.get("/{externalTaskKey}/summary", response_model=TaskSummaryResponse)
def task_summary(
    externalTaskKey: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    task = require_task_access(externalTaskKey, db, current_user)

    snapshot = (
        db.query(TaskSnapshot)
        .filter_by(external_task_key=task.external_task_key)
        .order_by(TaskSnapshot.created_at.desc())
        .first()
    )

    return TaskSummaryResponse(
        externalTaskKey=task.external_task_key,
        snapshotVersion=snapshot.snapshot_version if snapshot else None,
        taskContext=snapshot.task_context_json if snapshot else None,
        status=task.status,
        updatedAt=task.updated_at,
        # Include sync status for frontend polling
        syncStatus=task.sync_status,
        syncStartedAt=task.sync_started_at,
        syncCompletedAt=task.sync_completed_at,
        syncError=task.sync_error,
    )


@router.get("/{externalTaskKey}/sources", response_model=TaskSourcesResponse)
def task_sources(
    externalTaskKey: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    task = require_task_access(externalTaskKey, db, current_user)

    snapshot = (
        db.query(TaskSnapshot)
        .filter_by(external_task_key=task.external_task_key)
        .order_by(TaskSnapshot.created_at.desc())
        .first()
    )

    if snapshot is None:
        return TaskSourcesResponse(snapshotVersion=None, files=[])

    files = (
        db.query(TaskFile)
        .filter_by(
            external_task_key=task.external_task_key,
            snapshot_id=snapshot.id,
        )
        .order_by(TaskFile.created_at.asc())
        .all()
    )

    return TaskSourcesResponse(
        snapshotVersion=snapshot.snapshot_version,
        files=[
            TaskSourceFile(
                id=str(file.id),
                kind=file.kind,
                originalFilename=file.original_filename,
                mimeType=file.mime_type,
                sizeBytes=file.size_bytes,
                mondayAssetId=file.monday_asset_id,
                createdAt=file.created_at,
            )
            for file in files
        ],
    )


@router.get("/{externalTaskKey}/files/{fileId}/signed-url", response_model=SignedUrlResponse)
def file_signed_url(
    externalTaskKey: str,
    fileId: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    task = require_task_access(externalTaskKey, db, current_user)

    file_record = (
        db.query(TaskFile)
        .filter(
            TaskFile.id == fileId,
            TaskFile.external_task_key == task.external_task_key,
            TaskFile.deleted_at.is_(None),
        )
        .one_or_none()
    )
    if file_record is None:
        raise HTTPException(status_code=404, detail="File not found")

    expires_in = 3600
    signed = supabase.storage.from_(file_record.bucket).create_signed_url(
        file_record.object_path,
        expires_in,
    )
    url = _signed_url_from_response(signed)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    return SignedUrlResponse(url=url, expiresAt=expires_at)