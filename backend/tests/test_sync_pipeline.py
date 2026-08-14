from __future__ import annotations

import io
import os
import sys
from types import ModuleType
from types import SimpleNamespace
import uuid
import logging

import httpx
import pytest
import requests
from tenacity import wait_none

from backend.app.models import Task, TaskSnapshot

sys.modules.setdefault("extract_msg", ModuleType("extract_msg"))

from backend.app.services import storage_ingest, sync_pipeline


class FakeQuery:
    def __init__(self, result=None):
        self.result = result

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self.result


class FakeDB:
    def __init__(self, task: Task, snapshot: TaskSnapshot | None = None):
        self.task = task
        self.snapshot = snapshot
        self.committed = False
        self.commit_count = 0

    def get(self, model, key):
        if model is Task and key == self.task.external_task_key:
            return self.task
        return None

    def query(self, model):
        if model is TaskSnapshot:
            return FakeQuery(self.snapshot)
        return FakeQuery()

    def add(self, obj):
        if isinstance(obj, TaskSnapshot):
            obj.id = uuid.uuid4()
            self.snapshot = obj

    def flush(self):
        pass

    def commit(self):
        self.committed = True
        self.commit_count += 1


@pytest.mark.parametrize(
    ("api_key", "expected_authorization"),
    [
        ("sb_secret_test-key", None),
        ("eyJhbGciOiJIUzI1NiJ9.payload.signature", "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"),
    ],
)
def test_storage_upload_uses_key_compatible_auth_headers(
    monkeypatch,
    api_key,
    expected_authorization,
):
    request = httpx.Request("POST", "https://example.supabase.co/storage/v1/object/raw-monday/file.pdf")
    response = httpx.Response(200, request=request)
    uploaded_headers = []

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def post(self, url, content, headers):
            uploaded_headers.append(headers)
            return response

    monkeypatch.setattr(storage_ingest.settings, "supabase_service_role_key", api_key)
    monkeypatch.setattr(storage_ingest.httpx, "Client", FakeClient)

    storage_ingest.upload_with_retry(
        "raw-monday",
        "monday/account/board/item/snapshot/asset/file.pdf",
        b"pdf-content",
        "application/pdf",
    )

    assert uploaded_headers[0]["apikey"] == api_key
    assert uploaded_headers[0].get("Authorization") == expected_authorization


def test_storage_upload_logs_supabase_error_response(monkeypatch, caplog):
    request = httpx.Request("POST", "https://example.supabase.co/storage/v1/object/raw-monday/file.png")
    response = httpx.Response(
        400,
        request=request,
        json={"code": "InvalidMimeType", "message": "mime type image/png is not supported"},
        headers={
            "x-request-id": "storage-request-123",
            "server": "edge-proxy",
            "set-cookie": "private=value",
        },
    )

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def post(self, url, content, headers):
            return response

    monkeypatch.setattr(storage_ingest.httpx, "Client", FakeClient)

    with caplog.at_level(logging.ERROR), pytest.raises(httpx.HTTPStatusError):
        storage_ingest.upload_with_retry(
            "raw-monday",
            "monday/account/board/item/snapshot/asset/file.png",
            b"image",
            "image/png",
        )

    assert "status=400" in caplog.text
    assert '"code": "InvalidMimeType"' in caplog.text
    assert '"message": "mime type image/png is not supported"' in caplog.text
    assert "storage-request-123" in caplog.text
    assert "edge-proxy" in caplog.text
    assert "set-cookie" not in caplog.text
    assert storage_ingest._is_retryable_storage_response(response) is False


def _ingest_test_records(monkeypatch):
    captured = []
    result = SimpleNamespace(id="file-record")

    def fake_upsert(db, **values):
        captured.append(values)
        return result

    monkeypatch.setattr(storage_ingest, "upsert_task_file", fake_upsert)
    return captured, result


def _ingest_test_task_and_snapshot():
    task = Task(
        external_task_key="acct:board:item",
        account_id="acct",
        board_id="board",
        item_id="item",
    )
    snapshot = TaskSnapshot(
        id=uuid.uuid4(),
        external_task_key=task.external_task_key,
        snapshot_version="snapshot",
        task_context_json={},
    )
    return task, snapshot


def test_ingest_asset_records_reported_oversize_without_download_or_upload(monkeypatch):
    task, snapshot = _ingest_test_task_and_snapshot()
    captured, expected = _ingest_test_records(monkeypatch)
    monkeypatch.setattr(storage_ingest.settings, "supabase_storage_max_object_bytes", 100)
    monkeypatch.setattr(
        storage_ingest,
        "download_asset_to_temp",
        lambda *args, **kwargs: pytest.fail("oversized metadata should prevent download"),
    )
    monkeypatch.setattr(
        storage_ingest,
        "upload_with_retry",
        lambda *args, **kwargs: pytest.fail("oversized metadata should prevent upload"),
    )

    result = storage_ingest.ingest_asset(
        object(),
        task,
        snapshot,
        {"id": "asset-1", "name": "large.zip", "file_size": 101},
        "attachment",
        "token",
    )

    assert result is expected
    assert captured[0]["size_bytes"] == 101
    assert captured[0]["storage_status"] == "unsupported"
    assert captured[0]["storage_error_code"] == "object_too_large"


def test_ingest_asset_records_actual_oversize_and_removes_download(monkeypatch, tmp_path):
    task, snapshot = _ingest_test_task_and_snapshot()
    captured, expected = _ingest_test_records(monkeypatch)
    temp_path = tmp_path / "large.zip"
    temp_path.write_bytes(b"oversized-content")
    downloaded = storage_ingest.DownloadedAsset(
        temp_path=str(temp_path),
        content_type="application/zip",
        sha256="asset-sha",
        size_bytes=101,
    )
    monkeypatch.setattr(storage_ingest.settings, "supabase_storage_max_object_bytes", 100)
    monkeypatch.setattr(
        storage_ingest,
        "upload_with_retry",
        lambda *args, **kwargs: pytest.fail("actual oversize should prevent upload"),
    )

    result = storage_ingest.ingest_asset(
        object(),
        task,
        snapshot,
        {"id": "asset-1", "name": "large.zip", "file_size": 10},
        "attachment",
        "token",
        downloaded=downloaded,
    )

    assert result is expected
    assert captured[0]["size_bytes"] == 101
    assert captured[0]["sha256"] == "asset-sha"
    assert captured[0]["storage_status"] == "unsupported"
    assert not temp_path.exists()


def test_ingest_asset_converts_supabase_entity_too_large_to_unsupported(monkeypatch, tmp_path):
    task, snapshot = _ingest_test_task_and_snapshot()
    captured, expected = _ingest_test_records(monkeypatch)
    temp_path = tmp_path / "large.zip"
    temp_path.write_bytes(b"content")
    downloaded = storage_ingest.DownloadedAsset(
        temp_path=str(temp_path),
        content_type="application/zip",
        sha256="asset-sha",
        size_bytes=7,
    )
    request = httpx.Request("POST", "https://example.supabase.co/storage/v1/object/raw-monday/file.zip")
    response = httpx.Response(
        400,
        request=request,
        json={
            "statusCode": "413",
            "error": "Payload too large",
            "message": "The object exceeded the maximum allowed size",
            "code": "EntityTooLarge",
        },
    )
    upload_attempts = 0

    def reject_upload(*args, **kwargs):
        nonlocal upload_attempts
        upload_attempts += 1
        raise httpx.HTTPStatusError("too large", request=request, response=response)

    monkeypatch.setattr(storage_ingest.settings, "supabase_storage_max_object_bytes", 100)
    monkeypatch.setattr(storage_ingest, "upload_with_retry", reject_upload)

    assert storage_ingest._is_storage_object_too_large_response(response) is True
    assert storage_ingest._is_retryable_storage_response(response) is False

    result = storage_ingest.ingest_asset(
        object(),
        task,
        snapshot,
        {"id": "asset-1", "name": "large.zip", "file_size": 7},
        "attachment",
        "token",
        downloaded=downloaded,
    )

    assert result is expected
    assert upload_attempts == 1
    assert captured[0]["storage_status"] == "unsupported"
    assert captured[0]["storage_error_code"] == "object_too_large"
    assert "EntityTooLarge" in captured[0]["storage_error_detail"]
    assert not temp_path.exists()


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 502, 503, 504])
def test_storage_upload_classifies_transient_statuses_for_retry(status_code):
    request = httpx.Request("POST", "https://example.supabase.co/storage/v1/object/raw-monday/file.png")
    response = httpx.Response(status_code, request=request, text="temporary failure")

    assert storage_ingest._is_retryable_storage_response(response) is True


def test_storage_upload_retries_aborted_request_with_same_content(monkeypatch, caplog):
    request = httpx.Request("POST", "https://example.supabase.co/storage/v1/object/raw-monday/file.png")
    responses = [
        httpx.Response(
            400,
            request=request,
            json={"code": "InvalidRequest", "message": "request aborted"},
        ),
        httpx.Response(200, request=request),
    ]
    uploaded_contents = []

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def post(self, url, content, headers):
            uploaded_contents.append(content)
            return responses.pop(0)

    monkeypatch.setattr(storage_ingest.httpx, "Client", FakeClient)
    upload_with_no_wait = storage_ingest.upload_with_retry.retry_with(wait=wait_none())

    with caplog.at_level(logging.WARNING, logger=storage_ingest.__name__):
        upload_with_no_wait(
            "raw-monday",
            "monday/account/board/item/snapshot/asset/file.png",
            io.BytesIO(b"image-content"),
            "image/png",
        )

    assert uploaded_contents == [b"image-content", b"image-content"]
    assert "status=400" in caplog.text
    assert not [
        record
        for record in caplog.records
        if record.name == storage_ingest.__name__ and record.levelno >= logging.ERROR
    ]


def test_storage_upload_retries_generic_html_bad_request(monkeypatch, caplog):
    request = httpx.Request("POST", "https://example.supabase.co/storage/v1/object/raw-monday/file.msg")
    responses = [
        httpx.Response(
            400,
            request=request,
            text="<html><head><title>400 Bad Request</title></head></html>",
        ),
        httpx.Response(200, request=request),
    ]
    uploaded_contents = []

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def post(self, url, content, headers):
            uploaded_contents.append(content)
            return responses.pop(0)

    monkeypatch.setattr(storage_ingest.httpx, "Client", FakeClient)
    upload_with_no_wait = storage_ingest.upload_with_retry.retry_with(wait=wait_none())

    with caplog.at_level(logging.WARNING, logger=storage_ingest.__name__):
        upload_with_no_wait(
            "raw-monday",
            "monday/account/board/item/snapshot/asset/file.msg",
            io.BytesIO(b"email-content"),
            "application/vnd.ms-outlook",
        )

    assert uploaded_contents == [b"email-content", b"email-content"]
    assert "status=400" in caplog.text
    assert "<html><head><title>400 Bad Request</title></head></html>" in caplog.text


def test_storage_upload_limits_generic_html_bad_request_retries(monkeypatch):
    request = httpx.Request("POST", "https://example.supabase.co/storage/v1/object/raw-monday/file.msg")
    response = httpx.Response(
        400,
        request=request,
        text="<html><head><title>400 Bad Request</title></head></html>",
    )
    upload_attempts = 0

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def post(self, url, content, headers):
            nonlocal upload_attempts
            upload_attempts += 1
            return response

    monkeypatch.setattr(storage_ingest.httpx, "Client", FakeClient)
    upload_with_no_wait = storage_ingest.upload_with_retry.retry_with(wait=wait_none())

    with pytest.raises(httpx.HTTPStatusError):
        upload_with_no_wait(
            "raw-monday",
            "monday/account/board/item/snapshot/asset/file.msg",
            b"email-content",
            "application/vnd.ms-outlook",
        )

    assert upload_attempts == 3


def test_email_pipeline_cleans_pdf_attachments_skipped_by_limit(monkeypatch, tmp_path):
    task = Task(
        external_task_key="acct:1882196103:item-1",
        account_id="acct",
        board_id="1882196103",
        item_id="item-1",
    )
    item = {
        "id": "item-1",
        "updated_at": "2026-07-15T12:00:00Z",
        "assets": [
            {
                "id": "email-1",
                "name": "project-email.msg",
                "file_extension": ".msg",
                "file_size": 100,
                "url": "https://example.invalid/email.msg",
            }
        ],
        "updates": [],
        "column_values": [],
    }

    email_path = tmp_path / "project-email.msg"
    email_path.write_bytes(b"email")
    attachment_paths = []

    def fake_process_email_content_to_temp(email_content, filename):
        attachments = []
        for idx in range(10):
            path = tmp_path / f"attachment-{idx}.pdf"
            path.write_bytes(b"pdf")
            attachment_paths.append(path)
            attachments.append({"filename": f"attachment-{idx}.pdf", "temp_path": str(path)})
        return "", "", attachments, []

    monkeypatch.setattr(sync_pipeline, "fetch_item_with_assets", lambda access_token, item_id: item)
    monkeypatch.setattr(
        sync_pipeline,
        "download_asset_to_temp",
        lambda asset, access_token: SimpleNamespace(
            temp_path=str(email_path),
            size_bytes=email_path.stat().st_size,
            content_type="application/vnd.ms-outlook",
            sha256="sha256",
        ),
    )
    monkeypatch.setattr(sync_pipeline, "process_email_content_to_temp", fake_process_email_content_to_temp)
    monkeypatch.setattr(sync_pipeline, "ingest_asset", lambda *args, **kwargs: SimpleNamespace(id=None))
    monkeypatch.setattr(sync_pipeline, "ingest_derived_attachment_bytes", lambda *args, **kwargs: SimpleNamespace(id=None))
    monkeypatch.setattr(sync_pipeline, "process_pdf_batch", lambda pdfs: "extracted text")

    result = sync_pipeline.run_sync_pipeline(FakeDB(task), task.external_task_key, "token")

    assert result.status == "done"
    assert all(not path.exists() for path in attachment_paths)


def test_pipeline_returns_unchanged_only_for_complete_snapshot(monkeypatch):
    task = Task(
        external_task_key="acct:board:item-complete",
        account_id="acct",
        board_id="board",
        item_id="item-complete",
    )
    item = {
        "id": task.item_id,
        "updated_at": "2026-07-27T10:00:00Z",
        "assets": [],
        "updates": [],
        "column_values": [],
    }
    snapshot = TaskSnapshot(
        id=uuid.uuid4(),
        external_task_key=task.external_task_key,
        snapshot_version=storage_ingest.compute_snapshot_version(item),
        task_context_json=item,
        ingestion_status="complete",
    )
    monkeypatch.setattr(sync_pipeline, "fetch_item_with_assets", lambda *args: item)

    result = sync_pipeline.run_sync_pipeline(
        FakeDB(task, snapshot),
        task.external_task_key,
        "token",
    )

    assert result.status == "unchanged"
    assert snapshot.ingestion_status == "complete"


def test_ai_data_pdf_preview_is_stored_without_csv_parsing_or_embedding(
    monkeypatch,
    tmp_path,
):
    task = Task(
        external_task_key="acct:board:item-preview",
        account_id="acct",
        board_id="board",
        item_id="item-preview",
    )
    filename = "AI_Data_Preview_item-preview_revision_pipeline.pdf"
    item = {
        "id": task.item_id,
        "updated_at": "2026-08-14T12:00:00Z",
        "assets": [
            {
                "id": "preview-asset",
                "name": filename,
                "file_extension": "pdf",
                "file_size": 12,
            }
        ],
        "updates": [],
        "column_values": [
            {
                "type": "file",
                "value": '{"files":[{"assetId":"preview-asset"}]}',
                "column": {"title": "AI Data"},
            }
        ],
    }
    preview_path = tmp_path / filename
    preview_path.write_bytes(b"%PDF-preview")
    ingested_kinds = []

    monkeypatch.setattr(sync_pipeline, "fetch_item_with_assets", lambda *args: item)
    monkeypatch.setattr(
        sync_pipeline,
        "download_asset_to_temp",
        lambda *args: SimpleNamespace(
            temp_path=str(preview_path),
            size_bytes=preview_path.stat().st_size,
            content_type="application/pdf",
            sha256="preview-sha",
        ),
    )
    monkeypatch.setattr(
        sync_pipeline,
        "ingest_asset",
        lambda *args, **kwargs: (
            ingested_kinds.append(args[4]) or SimpleNamespace(id="preview-file")
        ),
    )
    monkeypatch.setattr(
        sync_pipeline,
        "_parse_key_value_csv",
        lambda *args: pytest.fail("preview PDF must not use key-value CSV parsing"),
    )
    monkeypatch.setattr(
        sync_pipeline,
        "_parse_generic_csv",
        lambda *args: pytest.fail("preview PDF must not use generic CSV parsing"),
    )
    monkeypatch.setattr(
        sync_pipeline,
        "process_pdf_batch",
        lambda *args: pytest.fail("preview PDF must not create duplicate embeddings"),
    )

    db = FakeDB(task)
    result = sync_pipeline.run_sync_pipeline(db, task.external_task_key, "token")

    assert result.status == "done"
    assert ingested_kinds == ["ai_data_preview"]
    assert db.snapshot.task_context_json["csv_params"] == []


def test_pipeline_resumes_failed_snapshot_and_marks_it_complete(monkeypatch):
    task = Task(
        external_task_key="acct:board:item-retry",
        account_id="acct",
        board_id="board",
        item_id="item-retry",
    )
    item = {
        "id": task.item_id,
        "updated_at": "2026-07-27T11:00:00Z",
        "assets": [],
        "updates": [],
        "column_values": [],
    }
    snapshot = TaskSnapshot(
        id=uuid.uuid4(),
        external_task_key=task.external_task_key,
        snapshot_version=storage_ingest.compute_snapshot_version(item),
        task_context_json={"partial": True},
        ingestion_status="failed",
        ingestion_error="503 UNAVAILABLE",
    )
    db = FakeDB(task, snapshot)

    def fake_fetch_item(*args):
        assert db.commit_count == 1
        return item

    monkeypatch.setattr(sync_pipeline, "fetch_item_with_assets", fake_fetch_item)

    result = sync_pipeline.run_sync_pipeline(db, task.external_task_key, "token")

    assert result.status == "done"
    assert snapshot.ingestion_status == "complete"
    assert snapshot.ingestion_error is None
    assert snapshot.completed_at is not None
    assert task.latest_snapshot_version == snapshot.snapshot_version
    assert db.commit_count >= 2


def test_pipeline_completes_snapshot_with_unsupported_oversized_asset(monkeypatch):
    task = Task(
        external_task_key="acct:board:item-oversized",
        account_id="acct",
        board_id="board",
        item_id="item-oversized",
    )
    item = {
        "id": task.item_id,
        "updated_at": "2026-08-07T18:00:00Z",
        "assets": [
            {
                "id": "asset-oversized",
                "name": "large.zip",
                "file_size": 101,
            }
        ],
        "updates": [],
        "column_values": [
            {
                "type": "file",
                "value": '{"files":[{"assetId":"asset-oversized"}]}',
                "column": {"title": "Email"},
            }
        ],
    }
    db = FakeDB(task)
    captured = []

    def fake_upsert(db, **values):
        captured.append(values)
        return SimpleNamespace(id=uuid.uuid4(), storage_status=values["storage_status"])

    monkeypatch.setattr(sync_pipeline, "fetch_item_with_assets", lambda *args: item)
    monkeypatch.setattr(storage_ingest.settings, "supabase_storage_max_object_bytes", 100)
    monkeypatch.setattr(storage_ingest, "upsert_task_file", fake_upsert)
    monkeypatch.setattr(
        storage_ingest,
        "download_asset_to_temp",
        lambda *args, **kwargs: pytest.fail("oversized metadata should prevent download"),
    )
    monkeypatch.setattr(
        storage_ingest,
        "upload_with_retry",
        lambda *args, **kwargs: pytest.fail("oversized metadata should prevent upload"),
    )

    result = sync_pipeline.run_sync_pipeline(db, task.external_task_key, "token")

    assert result.status == "done"
    assert db.snapshot.ingestion_status == "complete"
    assert db.snapshot.ingestion_error is None
    assert db.snapshot.completed_at is not None
    assert task.latest_snapshot_version == result.snapshot_version
    assert captured[0]["storage_status"] == "unsupported"
    assert captured[0]["storage_error_code"] == "object_too_large"


def test_pipeline_memory_abort_does_not_publish_partial_snapshot(monkeypatch):
    task = Task(
        external_task_key="acct:board:item-oom",
        account_id="acct",
        board_id="board",
        item_id="item-oom",
    )
    item = {
        "id": task.item_id,
        "updated_at": "2026-07-27T12:00:00Z",
        "assets": [{"id": "asset-1", "name": "drawing.pdf"}],
        "updates": [],
        "column_values": [],
    }
    db = FakeDB(task)
    monkeypatch.setattr(sync_pipeline, "fetch_item_with_assets", lambda *args: item)
    monkeypatch.setattr(
        sync_pipeline.psutil,
        "Process",
        lambda: SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=4 * 1024 * 1024 * 1024)
        ),
    )

    with pytest.raises(RuntimeError, match="critical memory pressure"):
        sync_pipeline.run_sync_pipeline(db, task.external_task_key, "token")

    assert db.snapshot.ingestion_status == "building"
    assert task.latest_snapshot_version is None


def test_asset_download_retries_interrupted_stream_and_removes_partial_file(monkeypatch):
    temp_paths = []
    original_named_temporary_file = storage_ingest.tempfile.NamedTemporaryFile

    def tracking_named_temporary_file(*args, **kwargs):
        temp_file = original_named_temporary_file(*args, **kwargs)
        temp_paths.append(temp_file.name)
        return temp_file

    class FakeResponse:
        def __init__(self, chunks, content_length):
            self._chunks = chunks
            self.headers = {
                "content-type": "application/pdf",
                "content-length": str(content_length),
            }
            self.closed = False

        def iter_content(self, chunk_size):
            yield from self._chunks

        def close(self):
            self.closed = True

    def interrupted_chunks():
        yield b"partial"
        raise requests.exceptions.ChunkedEncodingError("stream interrupted")

    responses = [
        FakeResponse(interrupted_chunks(), 8),
        FakeResponse([b"complete"], 8),
    ]
    monkeypatch.setattr(storage_ingest.tempfile, "NamedTemporaryFile", tracking_named_temporary_file)
    monkeypatch.setattr(storage_ingest, "download_asset", lambda *args, **kwargs: responses.pop(0))

    download_with_no_wait = storage_ingest.download_asset_to_temp.retry_with(wait=wait_none())
    downloaded = download_with_no_wait(
        {"id": "asset-1", "url": "https://example.invalid/file.pdf"},
        "token",
    )

    try:
        assert len(temp_paths) == 2
        assert not os.path.exists(temp_paths[0])
        assert os.path.exists(downloaded.temp_path)
        assert open(downloaded.temp_path, "rb").read() == b"complete"
    finally:
        os.unlink(downloaded.temp_path)