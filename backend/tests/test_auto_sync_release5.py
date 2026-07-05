from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import Task, TaskChunk, TaskFile, TaskSnapshot
from backend.app.services.auto_sync_policy import AutoSyncPolicy
from backend.app.services.auto_sync_purge import (
    mark_expired_task_restoring,
    place_retention_hold,
    purge_expired_tasks_once,
    record_meaningful_access,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()


def _policy() -> AutoSyncPolicy:
    return AutoSyncPolicy(
        enabled=True,
        board_id="1882196103",
        active_group_ids=frozenset({"topics"}),
        excluded_group_ids=frozenset({"group_mkpbd6vy"}),
        completed_group_id="group_mkpbb3tx",
        retention_days=30,
        debounce_seconds=90,
        backfill_batch_size=10,
    )


def _db_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def _completed_task(*, purge_after: datetime | None = None) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        external_task_key="acct:1882196103:item-1",
        account_id="acct",
        board_id="1882196103",
        item_id="item-1",
        auto_sync_enabled=True,
        auto_sync_state="completed_retained",
        sync_status="completed",
        completed_at=now - timedelta(days=40),
        purge_after=purge_after or now - timedelta(days=1),
        latest_snapshot_version="rev-1",
        last_indexed_source_revision="rev-1",
    )


def _add_snapshot_file_and_chunk(db_session, task: Task, *, object_path: str = "object/path.pdf") -> TaskFile:
    snapshot = TaskSnapshot(
        id=uuid.uuid4(),
        external_task_key=task.external_task_key,
        snapshot_version="rev-1",
        task_context_json={"id": task.item_id, "name": "Retained item"},
    )
    file_record = TaskFile(
        id=uuid.uuid4(),
        external_task_key=task.external_task_key,
        snapshot_id=snapshot.id,
        kind="attachment_pdf",
        monday_asset_id="asset-1",
        original_filename="drawing.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        bucket="raw-monday",
        object_path=object_path,
        sha256="sha",
    )
    chunk = TaskChunk(
        id=uuid.uuid4(),
        file_id=file_record.id,
        page=1,
        section="page:1",
        chunk_text="drawing notes",
        embedding=[0.0] * 1536,
    )
    db_session.add_all([snapshot, file_record, chunk])
    return file_record


def test_purge_expired_completed_task_removes_heavy_data_and_keeps_tombstone(db_session):
    task = _completed_task()
    db_session.add(task)
    _add_snapshot_file_and_chunk(db_session, task)
    db_session.commit()
    removed_objects: list[tuple[str, str]] = []

    def remove_storage_object(bucket: str, object_path: str) -> None:
        removed_objects.append((bucket, object_path))

    result = purge_expired_tasks_once(
        db_session,
        dry_run=False,
        policy=_policy(),
        remove_storage_object=remove_storage_object,
        ignore_disabled=True,
    )

    db_session.refresh(task)

    assert result.scanned == 1
    assert result.purged == 1
    assert result.items[0].action == "expired"
    assert result.items[0].files_deleted == 1
    assert result.items[0].chunks_deleted == 1
    assert removed_objects == [("raw-monday", "object/path.pdf")]
    assert db_session.query(TaskFile).count() == 0
    assert db_session.query(TaskChunk).count() == 0
    assert db_session.query(TaskSnapshot).count() == 0
    assert task.auto_sync_state == "expired"
    assert task.raw_purged_at is not None
    assert task.latest_snapshot_version is None
    assert task.last_indexed_source_revision == "rev-1"
    assert task.last_sync_result == "skipped"


def test_purge_keeps_retryable_file_references_when_storage_delete_fails(db_session):
    task = _completed_task()
    db_session.add(task)
    first_file = _add_snapshot_file_and_chunk(db_session, task, object_path="ok.pdf")
    second_file = TaskFile(
        id=uuid.uuid4(),
        external_task_key=task.external_task_key,
        snapshot_id=first_file.snapshot_id,
        kind="attachment_pdf",
        monday_asset_id="asset-2",
        original_filename="missing.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        bucket="raw-monday",
        object_path="fail.pdf",
        sha256="sha-2",
    )
    db_session.add(second_file)
    db_session.commit()

    def remove_storage_object(bucket: str, object_path: str) -> None:
        if object_path == "fail.pdf":
            raise RuntimeError("storage unavailable")

    result = purge_expired_tasks_once(
        db_session,
        dry_run=False,
        policy=_policy(),
        remove_storage_object=remove_storage_object,
        ignore_disabled=True,
    )

    db_session.refresh(task)
    db_session.refresh(first_file)
    db_session.refresh(second_file)

    assert result.failed == 1
    assert result.items[0].action == "storage_failed"
    assert task.auto_sync_state == "storage_deleting"
    assert task.raw_purged_at is None
    assert first_file.deleted_at is not None
    assert first_file.delete_error is None
    assert second_file.deleted_at is None
    assert "storage unavailable" in second_file.delete_error
    assert db_session.query(TaskSnapshot).count() == 1
    assert db_session.query(TaskFile).count() == 2


def test_retention_hold_prevents_due_purge(db_session):
    task = _completed_task()
    db_session.add(task)
    _add_snapshot_file_and_chunk(db_session, task)
    place_retention_hold(db_session, task, held_by="app-user", reason="needed for review")
    db_session.commit()

    result = purge_expired_tasks_once(
        db_session,
        dry_run=False,
        policy=_policy(),
        remove_storage_object=lambda bucket, object_path: None,
        ignore_disabled=True,
    )

    db_session.refresh(task)

    assert result.scanned == 0
    assert task.auto_sync_state == "completed_retained"
    assert task.retention_hold is True
    assert task.retention_hold_by == "app-user"
    assert db_session.query(TaskFile).count() == 1


def test_meaningful_access_extends_completed_retention_without_passive_reads(db_session):
    now = datetime.now(timezone.utc)
    task = _completed_task(purge_after=now + timedelta(days=1))
    db_session.add(task)
    db_session.commit()

    record_meaningful_access(db_session, task, policy=_policy(), now=now + timedelta(hours=2))
    db_session.commit()
    db_session.refresh(task)

    assert task.last_meaningful_access_at == _db_datetime(now + timedelta(hours=2))
    assert task.purge_after == _db_datetime(now + timedelta(hours=2, days=30))


def test_expired_task_can_be_marked_for_restoration(db_session, monkeypatch):
    now = datetime.now(timezone.utc)
    task = _completed_task()
    task.auto_sync_state = "expired"
    task.raw_purged_at = now - timedelta(days=1)
    task.purge_after = now - timedelta(days=10)
    db_session.add(task)
    db_session.commit()

    monkeypatch.setattr(
        "backend.app.services.auto_sync_purge.policy_from_settings",
        lambda: _policy(),
    )

    mark_expired_task_restoring(db_session, task, now=now)
    db_session.commit()
    db_session.refresh(task)

    assert task.auto_sync_state == "completed_retained"
    assert task.raw_purged_at is None
    assert task.purge_after == _db_datetime(now + timedelta(days=30))