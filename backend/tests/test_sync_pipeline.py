from __future__ import annotations

import sys
from types import ModuleType
from types import SimpleNamespace
import uuid

from backend.app.models import Task, TaskSnapshot

sys.modules.setdefault("extract_msg", ModuleType("extract_msg"))

from backend.app.services import sync_pipeline


class FakeQuery:
    def filter_by(self, **kwargs):
        return self

    def first(self):
        return None


class FakeDB:
    def __init__(self, task: Task):
        self.task = task
        self.committed = False

    def get(self, model, key):
        if model is Task and key == self.task.external_task_key:
            return self.task
        return None

    def query(self, model):
        return FakeQuery()

    def add(self, obj):
        if isinstance(obj, TaskSnapshot):
            obj.id = uuid.uuid4()

    def flush(self):
        pass

    def commit(self):
        self.committed = True


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