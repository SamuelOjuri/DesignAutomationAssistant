import os
import hashlib
import json
import re
import io
import tempfile
import gc
import mimetypes
import logging
from typing import Any, Dict
from dataclasses import dataclass

from tenacity import (
    RetryCallState,
    retry,
    wait_exponential,
    wait_random_exponential,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
)
import requests
import httpx

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

from ..config import settings
from ..models import Task, TaskFile, TaskSnapshot
from ..supabase_client import supabase
from ..monday_client import TransientMondayAPIError, download_asset

logger = logging.getLogger(__name__)

_RETRYABLE_STORAGE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_STORAGE_UPLOAD_ATTEMPTS = 5
_MAX_GENERIC_HTML_UPLOAD_ATTEMPTS = 3
_RETRYABLE_BAD_REQUEST_MARKERS = (
    "aborted",
    "interrupted",
    "unexpected end of",
)
_STORAGE_RESPONSE_LOG_HEADERS = (
    "content-type",
    "x-request-id",
    "x-correlation-id",
    "sb-request-id",
    "cf-ray",
    "server",
    "via",
)


def _storage_error_detail(response: httpx.Response, limit: int = 2000) -> str:
    try:
        detail = json.dumps(response.json(), ensure_ascii=True)
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = response.text
    return detail[:limit]


def _storage_response_log_metadata(response: httpx.Response) -> Dict[str, str]:
    return {
        header: response.headers[header][:500]
        for header in _STORAGE_RESPONSE_LOG_HEADERS
        if header in response.headers
    }


def _is_generic_html_storage_response(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    body_start = response.text.lstrip()[:200].lower()
    return "text/html" in content_type or body_start.startswith(
        ("<!doctype html", "<html")
    )


def _is_retryable_storage_response(response: httpx.Response) -> bool:
    if response.status_code in _RETRYABLE_STORAGE_STATUS_CODES:
        return True
    if response.status_code != 400:
        return False
    if _is_generic_html_storage_response(response):
        return True
    detail = _storage_error_detail(response).lower()
    return any(marker in detail for marker in _RETRYABLE_BAD_REQUEST_MARKERS)


def _is_retryable_storage_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and _is_retryable_storage_response(
        exc.response
    )


def _stop_storage_upload_retry(retry_state: RetryCallState) -> bool:
    if retry_state.attempt_number >= _MAX_STORAGE_UPLOAD_ATTEMPTS:
        return True
    if retry_state.attempt_number < _MAX_GENERIC_HTML_UPLOAD_ATTEMPTS:
        return False
    outcome = retry_state.outcome
    if outcome is None or not outcome.failed:
        return False
    exc = outcome.exception()
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 400
        and _is_generic_html_storage_response(exc.response)
    )


@retry(
    stop=_stop_storage_upload_retry,
    wait=wait_random_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception(_is_retryable_storage_error),
    reraise=True,
)
def upload_with_retry(bucket: str, path: str, file_content: Any, content_type: str):
    """
    Uploads file directly using httpx to allow for custom timeouts.
    """
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{path}"
    api_key = settings.supabase_service_role_key
    headers = {
        "apikey": api_key,
        "Content-Type": content_type,
        "x-upsert": "true"
    }
    if not api_key.startswith("sb_"):
        headers["Authorization"] = f"Bearer {api_key}"
    
    # timeout=300.0 means 5 minutes for connect/read/write/pool
    with httpx.Client(timeout=300.0) as client:
        # Check if file_content is bytes or a file-like object
        if hasattr(file_content, "read"):
            if hasattr(file_content, "seek"):
                file_content.seek(0)
            content = file_content.read()
        else:
            content = file_content
            
        response = client.post(url, content=content, headers=headers)
        if response.is_error:
            log_upload_failure = (
                logger.warning if _is_retryable_storage_response(response) else logger.error
            )
            log_upload_failure(
                "Supabase storage upload failed: status=%s bucket=%s path=%s response_headers=%s response=%s",
                response.status_code,
                bucket,
                path,
                _storage_response_log_metadata(response),
                _storage_error_detail(response),
            )
        response.raise_for_status()

def sanitize_filename(name: str) -> str:
    cleaned = name.strip().replace("\\", "_").replace("/", "_")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned)
    return cleaned or "file"

def build_object_path(
    account_id: str,
    board_id: str,
    item_id: str,
    snapshot_version: str,
    asset_id: str,
    filename: str,
) -> str:
    return (
        f"monday/{account_id}/{board_id}/{item_id}/"
        f"{snapshot_version}/{asset_id}/{filename}"
    )

def compute_snapshot_version(item: Dict[str, Any]) -> str:
    updated_at = item.get("updated_at") or ""
    asset_ids = set()
    for asset in item.get("assets") or []:
        if asset.get("id") is not None:
            asset_ids.add(str(asset.get("id")))
    for update in item.get("updates") or []:
        for asset in update.get("assets") or []:
            if asset.get("id") is not None:
                asset_ids.add(str(asset.get("id")))
    seed = f"{updated_at}:{','.join(sorted(asset_ids))}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()

def _kind_from_title(title: str) -> str:
    normalized = title.strip().lower()
    if normalized == "email":
        return "email"
    if normalized in {"ai data", "ai_data"}:
        return "csv"
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return slug or "attachment"

def extract_asset_kinds(column_values: list[Dict[str, Any]]) -> Dict[str, str]:
    kinds: Dict[str, str] = {}
    for col in column_values:
        if col.get("type") != "file":
            continue
        raw = col.get("value")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        title = (col.get("column") or {}).get("title") or ""
        kind = _kind_from_title(title)
        for f in data.get("files", []):
            asset_id = f.get("assetId")
            if asset_id is not None:
                kinds[str(asset_id)] = kind
    return kinds

def upsert_task_file(
    db: Session,
    *,
    external_task_key: str,
    snapshot_id: str,
    kind: str,
    monday_asset_id: str,
    original_filename: str,
    mime_type: str | None,
    size_bytes: int | None,
    bucket: str,
    object_path: str,
    sha256: str | None,
) -> TaskFile:
    values = {
        "external_task_key": external_task_key,
        "snapshot_id": snapshot_id,
        "kind": kind,
        "monday_asset_id": monday_asset_id,
        "original_filename": original_filename,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "bucket": bucket,
        "object_path": object_path,
        "sha256": sha256,
    }

    stmt = (
        insert(TaskFile)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["external_task_key", "snapshot_id", "monday_asset_id"],
            set_={
                "kind": kind,
                "original_filename": original_filename,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "bucket": bucket,
                "object_path": object_path,
                "sha256": sha256,
                "deleted_at": None,
                "delete_error": None,
            },
        )
        .returning(TaskFile.id)
    )

    result = db.execute(stmt)
    record_id = result.scalar_one()
    return db.get(TaskFile, record_id)

def ingest_asset(
    db: Session,
    task: Task,
    snapshot: TaskSnapshot,
    asset: Dict[str, Any],
    kind: str,
    access_token: str,
    downloaded: "DownloadedAsset | None" = None,
) -> TaskFile:
    asset_id = str(asset.get("id"))
    if not asset_id:
        raise HTTPException(status_code=502, detail="Asset missing id")

    filename = sanitize_filename(asset.get("name") or f"asset_{asset_id}")
    object_path = build_object_path(
        task.account_id,
        task.board_id,
        task.item_id,
        snapshot.snapshot_version,
        asset_id,
        filename,
    )

    if downloaded is None:
        downloaded = download_asset_to_temp(asset, access_token)

    try:
        with open(downloaded.temp_path, "rb") as f:
            upload_with_retry(
                settings.supabase_storage_bucket,
                object_path,
                f,
                downloaded.content_type,
            )
    finally:
        try:
            os.unlink(downloaded.temp_path)
        except OSError:
            pass

    return upsert_task_file(
        db,
        external_task_key=task.external_task_key,
        snapshot_id=str(snapshot.id),
        kind=kind,
        monday_asset_id=asset_id,
        original_filename=asset.get("name") or filename,
        mime_type=downloaded.content_type,
        size_bytes=downloaded.size_bytes or asset.get("file_size"),
        bucket=settings.supabase_storage_bucket,
        object_path=object_path,
        sha256=downloaded.sha256,
    )

def ingest_item_assets(
    db: Session,
    task: Task,
    snapshot: TaskSnapshot,
    item: Dict[str, Any],
    access_token: str,
) -> list[TaskFile]:
    asset_kinds = extract_asset_kinds(item.get("column_values") or [])
    files: list[TaskFile] = []
    seen: set[str] = set()

    def add_asset(asset: Dict[str, Any], kind: str) -> None:
        asset_id = str(asset.get("id") or "")
        if not asset_id or asset_id in seen:
            return
        seen.add(asset_id)
        files.append(ingest_asset(db, task, snapshot, asset, kind, access_token))

    for asset in item.get("assets") or []:
        asset_id = str(asset.get("id"))
        kind = asset_kinds.get(asset_id, "attachment")
        add_asset(asset, kind)

    for update in item.get("updates") or []:
        for asset in update.get("assets") or []:
            add_asset(asset, "update_attachment")

    return files

@dataclass
class DownloadedAsset:
    temp_path: str
    content_type: str
    sha256: str
    size_bytes: int


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception_type(
        (
            TransientMondayAPIError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        )
    ),
    reraise=True,
)
def download_asset_to_temp(asset: Dict[str, Any], access_token: str) -> DownloadedAsset:
    # Changed url selection logic to prefer 'public_url' if present, otherwise use 'url'
    url = asset.get("public_url") or asset.get("url")
    if not url:
        raise HTTPException(status_code=502, detail="Asset missing download url")

    # Changed use_token logic to match above: only use access_token if using the private 'url'
    use_token = access_token if url == asset.get("url") else None
    resp = download_asset(url, access_token=use_token)

    content_type = resp.headers.get("content-type") or "application/octet-stream"
    content_length = resp.headers.get("content-length")
    expected_size = int(content_length) if content_length and content_length.isdigit() else None
    sha = hashlib.sha256()
    size = 0

    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            sha.update(chunk)
            size += len(chunk)
            tmp.write(chunk)
        tmp.flush()
        if expected_size is not None and size != expected_size:
            raise requests.exceptions.ChunkedEncodingError(
                f"monday asset download incomplete: expected {expected_size} bytes, received {size}"
            )
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    finally:
        resp.close()
        tmp.close()

    return DownloadedAsset(
        temp_path=tmp.name,
        content_type=content_type,
        sha256=sha.hexdigest(),
        size_bytes=size,
    )

def attachment_kind_for_filename(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return "attachment_pdf"
    if lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")):
        return "attachment_image"
    return "attachment_other"

def ingest_derived_attachment_bytes(
    db: Session,
    task: Task,
    snapshot: TaskSnapshot,
    parent_asset_id: str,
    filename: str,
    content: bytes,
    kind: str | None = None,
    mime_type: str | None = None,
) -> TaskFile:
    import logging
    logger = logging.getLogger(__name__)

    content_size = len(content)
    logger.info(f"[INGEST] Starting upload: {filename}, size: {content_size / (1024*1024):.2f} MB")

    safe_name = sanitize_filename(filename)
    sha = hashlib.sha256(content).hexdigest()
    asset_id = f"derived:{parent_asset_id}:{sha[:12]}:{safe_name}"
    object_path = build_object_path(
        task.account_id,
        task.board_id,
        task.item_id,
        snapshot.snapshot_version,
        asset_id,
        safe_name,
    )
    mime_type = mime_type or (mimetypes.guess_type(safe_name)[0] or "application/octet-stream")

    logger.info(f"[INGEST] Uploading to path: {object_path}")

    upload_with_retry(
        settings.supabase_storage_bucket,
        object_path,
        content,
        mime_type,
    )

    logger.info(f"[INGEST] Upload complete: {filename}")

    result = upsert_task_file(
        db,
        external_task_key=task.external_task_key,
        snapshot_id=str(snapshot.id),
        kind=kind or attachment_kind_for_filename(filename),
        monday_asset_id=asset_id,
        original_filename=filename,
        mime_type=mime_type,
        size_bytes=content_size,
        bucket=settings.supabase_storage_bucket,
        object_path=object_path,
        sha256=sha,
    )

    # Force garbage collection for large files (>10MB)
    if content_size > 10 * 1024 * 1024:
        gc.collect()
        logger.info(f"[INGEST] Ran gc.collect() for large file: {filename}")

    return result