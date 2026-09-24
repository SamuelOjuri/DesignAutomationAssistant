"""Independent CRM refresh queue; UI and chat share the same committed values."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import MondayMetadataLink, Task, TaskMondayMetadata
from ..monday_client import fetch_current_account_id, fetch_monday_metadata
from .auto_sync_policy import policy_from_settings
from .monday_metadata_fields import (
    COLUMN_IDS, COLUMN_TITLES, LINKED_BOARD_IDS, MetadataReadError,
    metadata_revision, normalize_fields,
)
from .sync_asset_reuse import public_task_context

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _locked_record(db: Session, external_task_key: str):
    query = db.query(TaskMondayMetadata).filter_by(external_task_key=external_task_key).populate_existing()
    return query.with_for_update() if db.get_bind().dialect.name == "postgresql" else query


def enqueue_metadata(
    db: Session, task: Task, *, immediate: bool = False, only_if_idle: bool = False,
) -> TaskMondayMetadata:
    db.flush([task])
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    db.execute(insert(TaskMondayMetadata).values(
        external_task_key=task.external_task_key, requested_generation=0, completed_generation=0,
        attempt_count=0,
    ).on_conflict_do_nothing(index_elements=["external_task_key"]))
    record = _locked_record(db, task.external_task_key).one()
    if only_if_idle and record.requested_generation > record.completed_generation:
        return record
    record.requested_generation += 1
    record.scheduled_for = utc_now() + timedelta(
        seconds=0 if immediate else policy_from_settings().debounce_seconds,
    )
    record.attempt_count = 0
    return record


def enqueue_linked_dependents(db: Session, board_id: str, item_id: str) -> int:
    policy = policy_from_settings()
    if not policy.enabled or board_id not in LINKED_BOARD_IDS:
        return 0
    tasks = (
        db.query(Task).join(MondayMetadataLink, MondayMetadataLink.external_task_key == Task.external_task_key)
        .filter(
            MondayMetadataLink.linked_board_id == board_id,
            MondayMetadataLink.linked_item_id == item_id,
            Task.board_id == policy.board_id, Task.auto_sync_state == "active",
            Task.auto_sync_enabled.is_(True), Task.source_group_id.in_(policy.active_group_ids),
        ).order_by(Task.external_task_key).all()
    )
    for task in tasks:
        enqueue_metadata(db, task)
    return len(tasks)


def cancel_metadata(db: Session, task: Task) -> None:
    record = _locked_record(db, task.external_task_key).one_or_none()
    if record is not None:
        record.completed_generation = record.requested_generation
        record.scheduled_for = None
        record.lease_token = None
        record.lease_until = None
        record.last_error = None


def claim_refresh(db: Session, *, account_id: str) -> tuple[str, str, int] | None:
    now = utc_now()
    policy = policy_from_settings()
    query = db.query(TaskMondayMetadata).join(Task, Task.external_task_key == TaskMondayMetadata.external_task_key).filter(
        TaskMondayMetadata.requested_generation > TaskMondayMetadata.completed_generation,
        TaskMondayMetadata.scheduled_for <= now,
        or_(TaskMondayMetadata.lease_until.is_(None), TaskMondayMetadata.lease_until <= now),
        Task.account_id == account_id, Task.board_id == policy.board_id,
        Task.auto_sync_state == "active", Task.auto_sync_enabled.is_(True),
        Task.source_group_id.in_(policy.active_group_ids),
    ).order_by(TaskMondayMetadata.scheduled_for, TaskMondayMetadata.external_task_key)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True, of=TaskMondayMetadata)
    record = query.first()
    if record is None:
        db.commit()
        return None
    token = str(uuid.uuid4())
    record.lease_token = token
    record.lease_until = now + timedelta(seconds=LEASE_SECONDS)
    claim = (record.external_task_key, token, record.requested_generation)
    db.commit()
    return claim


def execute_refresh(db: Session, claim: tuple[str, str, int], access_token: str) -> str:
    task_key, lease_token, generation = claim
    task = db.get(Task, task_key)
    if task is None:
        return "missing"
    item_id, board_id = task.item_id, task.board_id
    db.commit()
    try:
        item = fetch_monday_metadata(access_token, item_id)
        if str(item.get("id")) != item_id or str((item.get("board") or {}).get("id")) != board_id:
            raise MetadataReadError("Monday returned an unexpected task identity")
        group_id = (item.get("group") or {}).get("id")
        if item.get("state") not in {"active", "archived", "deleted"} or not group_id:
            raise MetadataReadError("Monday task lifecycle is unavailable")
        eligible = item["state"] == "active" and policy_from_settings().classify_group(board_id, group_id).should_queue_sync
        fields = normalize_fields(item) if eligible else None
        # Match the orchestrator's Task -> metadata lock order. Inserting linked
        # dependencies takes a foreign-key lock on Task, so locking metadata first
        # could deadlock with an incoming event that already holds the Task lock.
        task_query = db.query(Task).filter_by(external_task_key=task_key).populate_existing()
        if db.get_bind().dialect.name == "postgresql":
            task_query = task_query.with_for_update()
        current_task = task_query.one_or_none()
        if current_task is None:
            db.rollback()
            return "missing"
        if current_task.auto_sync_state != "active" or not current_task.auto_sync_enabled:
            eligible, fields = False, None
        record = _locked_record(db, task_key).one()
        if record.lease_token != lease_token:
            db.rollback()
            return "lease_lost"
        record.lease_token = None
        record.lease_until = None
        # A change arrived while this read was in flight. Re-read after its debounce;
        # do not publish a response from the superseded generation.
        if record.requested_generation != generation:
            db.commit()
            return "superseded"
        now = utc_now()
        if fields is not None:
            revision = metadata_revision(fields)
            if record.revision != revision:
                record.fields_json = fields
                record.revision = revision
                record.changed_at = now
            record.checked_at = now
            db.query(MondayMetadataLink).filter_by(external_task_key=task_key).delete(synchronize_session=False)
            for field in fields:
                for linked in field["linkedItems"]:
                    db.add(MondayMetadataLink(
                        external_task_key=task_key, column_id=field["columnId"],
                        linked_board_id=linked["boardId"], linked_item_id=linked["id"],
                    ))
        record.completed_generation = generation
        record.scheduled_for = None
        record.last_error = None
        record.attempt_count = 0
        db.commit()
        logger.info("Monday metadata refreshed task=%s generation=%s eligible=%s", task_key, generation, eligible)
        return "completed" if eligible else "ineligible"
    except Exception as exc:
        db.rollback()
        record = _locked_record(db, task_key).one_or_none()
        if record is None or record.lease_token != lease_token:
            db.rollback()
            return "lease_lost"
        record.lease_token = None
        record.lease_until = None
        if record.requested_generation == generation:
            record.attempt_count += 1
            # Bounded backoff, durable retries; reconciliation and new edits can
            # request another check without deleting the last good values.
            record.scheduled_for = utc_now() + timedelta(seconds=min(3600, 30 * 2 ** min(record.attempt_count, 7)))
            record.last_error = "Monday details could not be refreshed. Retrying."
        db.commit()
        logger.warning("Monday metadata refresh failed task=%s error_type=%s", task_key, type(exc).__name__)
        return "failed"


def run_metadata_once(db: Session, access_token: str, *, limit: int = 20) -> list[str]:
    policy = policy_from_settings()
    if not policy.enabled:
        return []
    now = utc_now()
    due = db.query(TaskMondayMetadata.external_task_key).join(
        Task, Task.external_task_key == TaskMondayMetadata.external_task_key,
    ).filter(
        Task.board_id == policy.board_id, Task.auto_sync_state == "active",
        Task.auto_sync_enabled.is_(True), Task.source_group_id.in_(policy.active_group_ids),
        TaskMondayMetadata.requested_generation > TaskMondayMetadata.completed_generation,
        TaskMondayMetadata.scheduled_for <= now,
        or_(TaskMondayMetadata.lease_until.is_(None), TaskMondayMetadata.lease_until <= now),
    ).first()
    db.commit()
    if due is None:
        return []
    account_id = fetch_current_account_id(access_token)
    results = []
    for _ in range(limit):
        claim = claim_refresh(db, account_id=account_id)
        if claim is None:
            break
        results.append(execute_refresh(db, claim, access_token))
    return results


def resolve_task_context(db: Session, task_key: str, snapshot_context: dict[str, Any] | None) -> dict[str, Any] | None:
    record = db.get(TaskMondayMetadata, task_key, populate_existing=True)
    context = deepcopy(public_task_context(snapshot_context)) if snapshot_context is not None else {}
    if record is None:
        return context if snapshot_context is not None else None
    metadata = {
        "revision": record.revision,
        "checkedAt": aware(record.checked_at).isoformat() if record.checked_at else None,
        "changedAt": aware(record.changed_at).isoformat() if record.changed_at else None,
        "refreshPending": record.requested_generation > record.completed_generation,
        "refreshError": record.last_error,
        "fields": deepcopy(record.fields_json) if record.fields_json is not None else [],
    }
    context["monday_metadata"] = metadata
    if record.revision:
        # Replace stale representations rather than providing conflicting copies
        # of the same Monday fields in the assistant's raw snapshot context.
        context["column_values"] = [
            col for col in context.get("column_values") or []
            if col.get("id") not in COLUMN_IDS and (col.get("column") or {}).get("title") not in COLUMN_TITLES
        ] + [
            {"id": field["columnId"], "column": {"title": field["title"]},
             "type": field["type"], "text": field["displayValue"], "display_value": field["displayValue"]}
            for field in metadata["fields"]
        ]
    return context


def current_columns_text(context: dict[str, Any]) -> str:
    metadata = context.get("monday_metadata") or {}
    lines = [f"Monday CRM | Last checked: {metadata.get('checkedAt') or 'Not yet checked'}"]
    if metadata.get("refreshError"):
        lines.append(metadata["refreshError"])
    for field in metadata.get("fields") or []:
        lines.append(f"Column: {field['title']} | Value: {field['displayValue'] or 'Not set'} | Source: Monday CRM")
    return "\n".join(lines) + "\n"
