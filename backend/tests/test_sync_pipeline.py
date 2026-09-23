from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import os
import sys
from types import ModuleType
from types import SimpleNamespace
import uuid
import logging
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest
import requests
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from tenacity import wait_none

from backend.app.db import Base
from backend.app.models import Task, TaskSnapshot, TaskChunk, TaskFile

sys.modules.setdefault("extract_msg", ModuleType("extract_msg"))

from backend.app.services import storage_ingest, sync_pipeline, sync_asset_reuse


class FakeQuery:
    def __init__(self, result=None):
        self.result = result

    def filter_by(self, **kwargs):
        return self

    def filter(self, *args):
        return self

    def order_by(self, *args):
        return self

    def delete(self, **kwargs):
        pass

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


@pytest.mark.parametrize("extension", [".xls", ".xlsx"])
@pytest.mark.parametrize("via_email", [True, False])
@pytest.mark.parametrize("corrupt", [False, True])
def test_spreadsheet_pipeline_stores_and_indexes_own_file(
    monkeypatch, tmp_path, extension, via_email, corrupt,
):
    import openpyxl

    filename = "Pricing Schedule" + extension
    if corrupt:
        content = b"broken workbook"
    elif extension == ".xls":
        content = (Path(__file__).parent / "fixtures/spreadsheets/pricing_schedule.xls").read_bytes()
    else:
        book = openpyxl.Workbook()
        book.active.title = "Pricing"
        book.active.append(["Item", "Price"])
        book.active.append(["Roof insulation", 25.5])
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        content = stream.getvalue()

    if via_email:
        message = EmailMessage()
        message["Subject"] = "Pricing enquiry"
        message.set_content("Please review the attached schedule.")
        message.add_attachment(content, maintype="application", subtype="octet-stream", filename=filename)
        # A second attachment proves unreadable Excel files do not stop the email.
        message.add_attachment(b"keep me", maintype="application", subtype="octet-stream", filename="notes.txt")
        download_content = message.as_bytes()
        asset_name = "enquiry.eml"
    else:
        download_content = content
        asset_name = filename
    task, _ = _ingest_test_task_and_snapshot()
    item = {"assets": [{"id": "asset-1", "name": asset_name}], "column_values": [], "updates": []}
    stored = []
    temp_paths = []
    spreadsheet_id = uuid.uuid4()

    class ChunkQuery(FakeQuery):
        def filter(self, expression):
            self.file_id = getattr(expression.right, "value", None)
            return self

        def delete(self, **kwargs):
            db.chunks[:] = [
                chunk for chunk in db.chunks
                if self.file_id is not None and chunk.file_id != self.file_id
            ]

    class ChunkDB(FakeDB):
        def __init__(self):
            super().__init__(task)
            self.chunks = []

        def query(self, model):
            return ChunkQuery() if model is TaskChunk else super().query(model)

        def add(self, obj):
            if isinstance(obj, TaskChunk):
                self.chunks.append(obj)
            else:
                super().add(obj)

    db = ChunkDB()

    def download(*args):
        path = tmp_path / "download"
        path.write_bytes(download_content)
        temp_paths.append(path)
        return SimpleNamespace(temp_path=str(path), size_bytes=len(download_content))

    def store_asset(*args, downloaded, **kwargs):
        path = Path(downloaded.temp_path)
        stored.append((asset_name, path.read_bytes(), args[4]))
        path.unlink()  # Real ingest_asset also deletes the download.
        return SimpleNamespace(id=uuid.uuid4() if via_email else spreadsheet_id)

    def store_attachment(*args, **kwargs):
        stored.append((kwargs["filename"], kwargs["content"], kwargs["kind"]))
        return SimpleNamespace(id=spreadsheet_id if kwargs["filename"] == filename else uuid.uuid4())

    real_email_extract = sync_pipeline.process_email_content_to_temp

    def extract_email(*args):
        result = real_email_extract(*args)
        temp_paths.extend(Path(att["temp_path"]) for att in result[2])
        return result

    monkeypatch.setattr(sync_pipeline, "fetch_item_with_assets", lambda *args: item)
    monkeypatch.setattr(sync_pipeline, "download_asset_to_temp", download)
    monkeypatch.setattr(sync_pipeline, "ingest_asset", store_asset)
    monkeypatch.setattr(sync_pipeline, "ingest_derived_attachment_bytes", store_attachment)
    monkeypatch.setattr(sync_pipeline, "process_email_content_to_temp", extract_email)
    monkeypatch.setattr(sync_pipeline, "create_gemini_client", lambda: object())
    monkeypatch.setattr(sync_pipeline, "gemini_embed_content_with_retry", lambda *args, **kwargs: SimpleNamespace(
        embeddings=[SimpleNamespace(values=[1.0, 0.0]) for _ in kwargs["contents"]],
    ))

    result = sync_pipeline.run_sync_pipeline(db, task.external_task_key, "token")
    assert result.status == "done"
    assert (filename, content, "attachment_spreadsheet") in stored
    if via_email:
        assert ("notes.txt", b"keep me", "attachment_other") in stored
    chunks = [chunk for chunk in db.chunks if chunk.file_id == spreadsheet_id]
    assert chunks
    if corrupt:
        assert chunks[0].section == "spreadsheet:extraction-notice"
        assert "could not be fully read" in chunks[0].chunk_text
    else:
        assert any("Roof insulation" in chunk.chunk_text and "25.5" in chunk.chunk_text for chunk in chunks)
        assert all(chunk.section.startswith("sheet:") for chunk in chunks)
    assert db.snapshot.task_context_json["extracted_docs_summary"]["by_kind"]["spreadsheet"] == len(chunks)
    assert all(not path.exists() for path in temp_paths)

    # A forced sync upgrades existing snapshots and replaces spreadsheet chunks.
    result = sync_pipeline.run_sync_pipeline(db, task.external_task_key, "token", force=True)
    assert result.status == "done"
    assert len([c for c in db.chunks if c.file_id == spreadsheet_id]) == len(chunks)
    assert all(not path.exists() for path in temp_paths)


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


@pytest.fixture()
def incremental_pipeline(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_uuid(connection, record):
        connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine)
    task, _ = _ingest_test_task_and_snapshot()
    item = {
        "id": task.item_id,
        "name": "Roof enquiry",
        "updated_at": "2026-09-21T08:16:14Z",
        "assets": [{"id": "drawing", "name": "roof.pdf"}],
        "updates": [],
        "column_values": [{"id": "priority", "column": {"title": "Priority"}, "text": "Low"}],
    }
    contents = {"drawing": b"%PDF-drawing"}
    calls = {"downloads": [], "uploads": [], "pdfs": [], "embeddings": []}

    def download(asset, access_token):
        content = contents[asset["id"]]
        path = tmp_path / str(uuid.uuid4())
        path.write_bytes(content)
        calls["downloads"].append(asset["id"])
        return storage_ingest.DownloadedAsset(
            temp_path=str(path), content_type="application/octet-stream",
            sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content),
        )

    def extract(pdfs):
        calls["pdfs"].extend(pdf["filename"] for pdf in pdfs)
        return "Roof specification"

    def embed(*args, **kwargs):
        calls["embeddings"].extend(kwargs["contents"])
        return SimpleNamespace(embeddings=[
            SimpleNamespace(values=[1.0] + [0.0] * 1535)
            for _ in kwargs["contents"]
        ])

    original_upsert = storage_ingest.upsert_task_file

    def upsert(db, **values):
        values["snapshot_id"] = uuid.UUID(str(values["snapshot_id"]))
        return original_upsert(db, **values)

    monkeypatch.setattr(storage_ingest, "upsert_task_file", upsert)
    monkeypatch.setattr(sync_pipeline, "fetch_item_with_assets", lambda *args: deepcopy(item))
    monkeypatch.setattr(sync_pipeline, "download_asset_to_temp", download)
    monkeypatch.setattr(storage_ingest, "download_asset_to_temp", download)
    monkeypatch.setattr(storage_ingest, "upload_with_retry", lambda bucket, path, *args: calls["uploads"].append(path))
    monkeypatch.setattr(sync_pipeline, "process_pdf_batch", extract)
    monkeypatch.setattr(sync_pipeline, "create_gemini_client", lambda: object())
    monkeypatch.setattr(sync_pipeline, "gemini_embed_content_with_retry", embed)
    monkeypatch.setattr(sync_pipeline.psutil, "Process", lambda: SimpleNamespace(
        memory_info=lambda: SimpleNamespace(rss=100 * 1024 * 1024),
    ))
    with Session(engine) as db:
        db.add(task)
        db.commit()
        yield SimpleNamespace(db=db, task=task, item=item, contents=contents, calls=calls, temp_path=tmp_path)
    engine.dispose()


def test_metadata_only_refresh_reuses_asset_results(incremental_pipeline):
    case = incremental_pipeline
    first = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    previous_calls = deepcopy(case.calls)
    case.item["updated_at"] = "2026-09-21T10:47:59Z"
    second = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")

    assert second.snapshot_version != first.snapshot_version
    assert second.snapshot_version == storage_ingest.compute_snapshot_version(case.item)
    assert case.calls["pdfs"] == previous_calls["pdfs"]
    assert case.calls["embeddings"] == previous_calls["embeddings"]
    assert case.calls["uploads"] == previous_calls["uploads"]
    assert case.calls["downloads"] == ["drawing", "drawing"]
    snapshots = case.db.query(TaskSnapshot).all()
    assert len(snapshots) == 2
    assert all(snapshot.ingestion_status == "complete" for snapshot in snapshots)
    for snapshot in snapshots:
        files = case.db.query(TaskFile).filter_by(snapshot_id=snapshot.id).all()
        assert len(files) == 2
        assert case.db.query(TaskChunk).filter(TaskChunk.file_id.in_([file.id for file in files])).count() == 2
    assert list(case.temp_path.iterdir()) == []


def test_metadata_changes_only_reembed_changed_columns(incremental_pipeline):
    case = incremental_pipeline
    sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    case.item["updated_at"] = "2026-09-23T09:00:00Z"
    case.item["name"] = "Renamed enquiry"
    case.item["column_values"][0]["text"] = "High"
    result = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")

    snapshot = case.db.query(TaskSnapshot).filter_by(snapshot_version=result.snapshot_version).one()
    assert snapshot.task_context_json["name"] == "Renamed enquiry"
    assert snapshot.task_context_json["column_values"][0]["text"] == "High"
    assert case.calls["pdfs"] == ["roof.pdf"]
    assert case.calls["embeddings"] == [
        "Roof specification", "Column: Priority | Value: Low", "Column: Priority | Value: High",
    ]
    assert len(case.calls["uploads"]) == 3


def test_reuse_manifest_is_private_to_ingestion(incremental_pipeline, monkeypatch):
    from backend.app.routes import tasks
    from backend.app.services.retrieval import get_task_context

    case = incremental_pipeline
    sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    monkeypatch.setattr(tasks, "require_task_access", lambda *args: case.task)
    summary = tasks.task_summary(case.task.external_task_key, db=case.db, current_user=None)
    context = get_task_context(case.db, case.task.external_task_key)

    assert sync_asset_reuse.MANIFEST_KEY not in summary.taskContext
    assert sync_asset_reuse.MANIFEST_KEY not in context
    assert context == summary.taskContext
    assert sync_asset_reuse.MANIFEST_KEY in case.db.query(TaskSnapshot).one().task_context_json


@pytest.mark.parametrize("change", ["bytes", "name", "role", "version", "legacy", "failed", "deleted", "missing_chunks"])
def test_asset_reuse_requires_verified_compatible_results(incremental_pipeline, monkeypatch, change):
    case = incremental_pipeline
    first = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    snapshot = case.db.query(TaskSnapshot).filter_by(snapshot_version=first.snapshot_version).one()
    file_record = case.db.query(TaskFile).filter_by(snapshot_id=snapshot.id, monday_asset_id="drawing").one()
    if change == "bytes":
        case.contents["drawing"] = b"%PDF-revised drawing"
    elif change == "name":
        case.item["assets"][0]["name"] = "renamed.pdf"
    elif change == "role":
        case.item["column_values"].append({
            "type": "file", "column": {"title": "Drawings"},
            "value": '{"files":[{"assetId":"drawing"}]}',
        })
    elif change == "version":
        monkeypatch.setattr(sync_asset_reuse, "PROCESSING_VERSION", "asset-results-v2")
    elif change == "legacy":
        context = deepcopy(snapshot.task_context_json)
        context.pop(sync_asset_reuse.MANIFEST_KEY)
        snapshot.task_context_json = context
    elif change == "failed":
        snapshot.ingestion_status = "failed"
    elif change == "deleted":
        file_record.deleted_at = snapshot.completed_at
    elif change == "missing_chunks":
        case.db.query(TaskChunk).filter_by(file_id=file_record.id).delete()
    case.db.commit()
    case.item["updated_at"] = "2026-09-23T09:00:00Z"

    sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")

    assert len(case.calls["pdfs"]) == 2
    assert list(case.temp_path.iterdir()) == []


def test_incremental_addition_and_removal_only_processes_new_asset(incremental_pipeline):
    case = incremental_pipeline
    sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    case.item["updated_at"] = "2026-09-23T09:00:00Z"
    case.item["updates"] = [{"id": "update", "assets": [{"id": "new", "name": "new.pdf"}]}]
    case.contents["new"] = b"%PDF-new drawing"
    sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    assert case.calls["pdfs"] == ["roof.pdf", "new.pdf"]
    case.item["updated_at"] = "2026-09-23T10:00:00Z"
    case.item["assets"] = []
    result = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")

    snapshot = case.db.query(TaskSnapshot).filter_by(snapshot_version=result.snapshot_version).one()
    files = case.db.query(TaskFile).filter_by(snapshot_id=snapshot.id).all()
    assert len(files) == 2
    assert not any(file.monday_asset_id == "drawing" for file in files)
    assert case.calls["pdfs"] == ["roof.pdf", "new.pdf"]
    assert snapshot.task_context_json["extracted_docs_summary"]["total_chunks"] == 2


def test_reuse_preserves_email_family_csv_and_vectors(incremental_pipeline):
    case = incremental_pipeline
    message = EmailMessage()
    message["Subject"] = "Roof enquiry"
    message.set_content("Please review this drawing")
    message.add_attachment(b"%PDF-attachment", maintype="application", subtype="pdf", filename="drawing.pdf")
    message.add_attachment(b"notes", maintype="text", subtype="plain", filename="notes.txt")
    case.item["assets"] = [
        {"id": "email", "name": "enquiry.eml"}, {"id": "csv", "name": "parameters.csv"},
    ]
    case.contents.update({"email": message.as_bytes(), "csv": b"Parameter,Value,Source\nU-Value,0.12,Email\n"})
    first = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    before = deepcopy(case.calls)
    case.item["updated_at"] = "2026-09-23T09:00:00Z"
    second = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    snapshots = [case.db.query(TaskSnapshot).filter_by(snapshot_version=version).one()
                 for version in (first.snapshot_version, second.snapshot_version)]

    assert snapshots[0].task_context_json["csv_params"] == snapshots[1].task_context_json["csv_params"]
    assert snapshots[0].task_context_json["extracted_docs_summary"] == snapshots[1].task_context_json["extracted_docs_summary"]
    file_sets = [case.db.query(TaskFile).filter_by(snapshot_id=snapshot.id).all() for snapshot in snapshots]
    assert len(file_sets[0]) == len(file_sets[1]) == 5
    assert {file.object_path for file in file_sets[0]} == {file.object_path for file in file_sets[1]}
    assert {file.id for file in file_sets[0]}.isdisjoint({file.id for file in file_sets[1]})
    chunks = [case.db.query(TaskChunk).filter(TaskChunk.file_id.in_([file.id for file in files]))
              .order_by(TaskChunk.chunk_text).all() for files in file_sets]
    assert [chunk.chunk_text for chunk in chunks[0]] == [chunk.chunk_text for chunk in chunks[1]]
    assert all((old.embedding == new.embedding).all() for old, new in zip(*chunks))
    for name in ("pdfs", "embeddings", "uploads"):
        assert case.calls[name] == before[name]


def test_force_reprocesses_without_overwriting_shared_objects(incremental_pipeline):
    case = incremental_pipeline
    first = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    case.item["updated_at"] = "2026-09-23T09:00:00Z"
    second = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    case.item["updated_at"] = "2026-09-21T08:16:14Z"
    case.contents["drawing"] = b"%PDF-changed without new asset id"
    forced = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token", force=True)

    assert forced.snapshot_version == first.snapshot_version
    assert len(case.calls["pdfs"]) == 2
    snapshots = [case.db.query(TaskSnapshot).filter_by(snapshot_version=version).one()
                 for version in (first.snapshot_version, second.snapshot_version)]
    files = [case.db.query(TaskFile).filter_by(snapshot_id=snapshot.id, monday_asset_id="drawing").one()
             for snapshot in snapshots]
    assert files[0].sha256 != files[1].sha256
    assert files[0].object_path != files[1].object_path
    assert case.calls["uploads"].count(files[1].object_path) == 1


def test_failed_extraction_is_not_reused(incremental_pipeline, monkeypatch):
    case = incremental_pipeline
    original = sync_pipeline.process_pdf_batch
    monkeypatch.setattr(sync_pipeline, "process_pdf_batch", lambda *args: "Error processing PDF: service unavailable")
    first = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    snapshot = case.db.query(TaskSnapshot).filter_by(snapshot_version=first.snapshot_version).one()
    assert "drawing" not in snapshot.task_context_json[sync_asset_reuse.MANIFEST_KEY]["assets"]
    monkeypatch.setattr(sync_pipeline, "process_pdf_batch", original)
    case.item["updated_at"] = "2026-09-23T09:00:00Z"

    sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")

    assert case.calls["pdfs"] == ["roof.pdf"]


def test_failed_refresh_keeps_previous_complete_snapshot(incremental_pipeline, monkeypatch):
    case = incremental_pipeline
    first = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    case.item["updated_at"] = "2026-09-23T09:00:00Z"
    case.item["assets"].append({"id": "new", "name": "new.pdf"})
    case.contents["new"] = b"%PDF-new"
    original_embed = sync_pipeline.gemini_embed_content_with_retry

    def fail_embedding(*args, **kwargs):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(sync_pipeline, "gemini_embed_content_with_retry", fail_embedding)
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    case.db.rollback()
    assert case.task.latest_snapshot_version == first.snapshot_version
    complete = case.db.query(TaskSnapshot).filter_by(ingestion_status="complete").all()
    assert len(complete) == 1
    assert complete[0].snapshot_version == first.snapshot_version
    assert list(case.temp_path.iterdir()) == []

    monkeypatch.setattr(sync_pipeline, "gemini_embed_content_with_retry", original_embed)
    result = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    snapshot = case.db.query(TaskSnapshot).filter_by(snapshot_version=result.snapshot_version).one()
    files = case.db.query(TaskFile).filter_by(snapshot_id=snapshot.id).all()
    assert len(files) == 3
    assert case.db.query(TaskChunk).filter(TaskChunk.file_id.in_([file.id for file in files])).count() == 3
    assert snapshot.ingestion_status == "complete"
    assert case.calls["pdfs"] == ["roof.pdf", "new.pdf", "new.pdf"]


def test_memory_guard_does_not_cache_incomplete_asset_results(incremental_pipeline, monkeypatch):
    case = incremental_pipeline
    monkeypatch.setattr(sync_pipeline.psutil, "Process", lambda: SimpleNamespace(
        memory_info=lambda: SimpleNamespace(rss=2800 * 1024 * 1024),
    ))
    result = sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    snapshot = case.db.query(TaskSnapshot).filter_by(snapshot_version=result.snapshot_version).one()
    assert snapshot.task_context_json[sync_asset_reuse.MANIFEST_KEY]["assets"] == {}
    monkeypatch.setattr(sync_pipeline.psutil, "Process", lambda: SimpleNamespace(
        memory_info=lambda: SimpleNamespace(rss=100 * 1024 * 1024),
    ))
    case.item["updated_at"] = "2026-09-23T09:00:00Z"
    sync_pipeline.run_sync_pipeline(case.db, case.task.external_task_key, "token")
    assert case.calls["pdfs"] == ["roof.pdf"]


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
