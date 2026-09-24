from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, literal, inspect
from sqlalchemy.orm import Session

from backend.app import monday_client
from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import Task, TaskSnapshot, TaskFile, TaskChunk, TaskMondayMetadata, MondayMetadataLink, AutoSyncJob
from backend.app.routes import tasks, monday_webhooks
from backend.app.services import monday_metadata as metadata, retrieval, auto_sync_reconciliation
from backend.app.services.auto_sync import apply_auto_sync_policy_for_item, compute_desired_source_revision
from backend.app.services.auto_sync_policy import policy_from_settings
from backend.app.services.monday_metadata_fields import FIELDS, normalize_fields, metadata_revision, MetadataReadError, only_crm_fields_changed


def monday_item():
    columns = []
    for key, column_id, title, kind in FIELDS:
        col = {"id": column_id, "column": {"title": title}, "type": kind, "text": None}
        if kind == "board_relation":
            linked = {"id": "501", "name": "Account, Limited", "board": {"id": "1654217230"}} if key == "accounts" else {
                "id": "601", "name": "0017671", "board": {"id": "1825117125"},
            }
            col.update(linked_items=[linked], linked_item_ids=[linked["id"]], display_value=linked["name"])
        elif kind == "dropdown":
            col["values"] = [{"id": "1", "label": "New Enquiry"}, {"id": "2", "label": "Amendment"}] if key == "enquiryType" else [{"id": "88", "label": "RH"}]
        else:
            col.update(display_value="Airport, South Terminal", mirrored_items=[{
                "linked_item": {"id": "601"}, "mirrored_value": {"text": "Airport, South Terminal"},
            }])
        columns.append(col)
    return {
        "id": "123", "account_id": "acct", "name": "Original email subject", "state": "active",
        "board": {"id": "1882196103"}, "group": {"id": "topics", "title": "Hub A"},
        "updated_at": "2026-09-24T12:00:00Z", "assets": [], "updates": [], "column_values": columns,
    }


@pytest.fixture()
def case(monkeypatch):
    monkeypatch.setattr(settings, "auto_sync_enabled", True)
    monkeypatch.setattr(settings, "auto_sync_board_id", "1882196103")
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics,group_mkpbs35c")
    monkeypatch.setattr(settings, "auto_sync_debounce_seconds", 90)
    engine = create_engine("sqlite://")
    @event.listens_for(engine, "connect")
    def register_uuid(connection, _):
        connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        task = Task(external_task_key="acct:1882196103:123", account_id="acct", board_id="1882196103", item_id="123",
                    auto_sync_state="active", auto_sync_enabled=True, source_group_id="topics", sync_status="completed")
        db.add(task)
        db.commit()
        item = monday_item()
        calls = []
        def fetch(token, item_id):
            calls.append(item_id)
            return deepcopy(item)
        monkeypatch.setattr(metadata, "fetch_monday_metadata", fetch)
        monkeypatch.setattr(metadata, "fetch_current_account_id", lambda token: "acct")
        yield SimpleNamespace(db=db, task=task, item=item, calls=calls)
    engine.dispose()


def refresh(case):
    metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    assert metadata.run_metadata_once(case.db, "token") == ["completed"]
    return case.db.get(TaskMondayMetadata, case.task.external_task_key, populate_existing=True)


def test_read_contract_resolves_ids_labels_mirror_and_multiple_selections():
    fields = normalize_fields(monday_item())
    assert fields[0]["displayValue"] == "Account, Limited"
    assert fields[1]["displayValue"] == "New Enquiry, Amendment"
    assert fields[2]["displayValue"] == "0017671"
    assert fields[2]["linkedItems"][0]["id"] == "601"
    assert fields[3]["values"] == [{"id": "601", "label": "Airport, South Terminal"}]
    assert fields[4]["displayValue"] == "RH"


def test_revision_uses_values_and_ignores_dropdown_order_column_titles_and_item_timestamp():
    original = monday_item()
    modified = deepcopy(original)
    modified["column_values"][1]["values"].reverse()
    modified["column_values"][0]["column"]["title"] = "Renamed Accounts column"
    modified["updated_at"] = "later"
    assert metadata_revision(normalize_fields(original)) == metadata_revision(normalize_fields(modified))
    modified["column_values"][3]["mirrored_items"][0]["mirrored_value"]["text"] = "New Project Name"
    assert metadata_revision(normalize_fields(original)) != metadata_revision(normalize_fields(modified))


@pytest.mark.parametrize("invalid", ["missing_column", "wrong_type", "unreadable_relation", "unresolved_mirror", "missing_dropdown"])
def test_incomplete_reads_are_not_treated_as_empty(invalid):
    item = monday_item()
    if invalid == "missing_column": item["column_values"].pop()
    if invalid == "wrong_type": item["column_values"][0]["type"] = "text"
    if invalid == "unreadable_relation": item["column_values"][0]["linked_items"] = []
    if invalid == "unresolved_mirror": item["column_values"][3]["mirrored_items"] = []
    if invalid == "missing_dropdown": del item["column_values"][1]["values"]
    with pytest.raises(MetadataReadError): normalize_fields(item)


def test_debounce_coalesces_and_worker_does_not_fetch_before_due(case, monkeypatch):
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(metadata, "utc_now", lambda: now)
    first = metadata.enqueue_metadata(case.db, case.task)
    assert first.scheduled_for == now + timedelta(seconds=90)
    now += timedelta(seconds=20)
    second = metadata.enqueue_metadata(case.db, case.task)
    assert second.requested_generation == 2
    assert second.scheduled_for == now + timedelta(seconds=90)
    case.db.commit()
    assert metadata.run_metadata_once(case.db, "token") == []
    assert case.calls == []
    now += timedelta(seconds=90)
    assert metadata.run_metadata_once(case.db, "token") == ["completed"]
    assert case.calls == ["123"]


def test_refresh_is_independent_of_building_document_snapshot(case):
    case.db.add(TaskSnapshot(id=uuid.uuid4(), external_task_key=case.task.external_task_key,
        snapshot_version="building", ingestion_status="building", task_context_json={}))
    case.task.sync_status = "syncing"
    record = refresh(case)
    assert record.revision
    assert case.task.sync_status == "syncing"
    assert case.db.query(TaskFile).count() == 0
    assert case.db.query(TaskChunk).count() == 0
    assert case.db.query(AutoSyncJob).count() == 0
    assert case.db.query(MondayMetadataLink).count() == 2


def test_cleared_fields_replace_old_values_and_remove_link_dependencies(case):
    old_revision = refresh(case).revision
    for col in case.item["column_values"]:
        if col["type"] == "board_relation": col.update(linked_items=[], linked_item_ids=[], display_value="")
        if col["type"] == "dropdown": col["values"] = []
        if col["type"] == "mirror": col.update(mirrored_items=[], display_value="")
    record = refresh(case)
    assert record.revision != old_revision
    assert all(field["state"] == "empty" and field["displayValue"] == "" for field in record.fields_json)
    assert case.db.query(MondayMetadataLink).count() == 0
    assert "Value: Not set" in metadata.current_columns_text(metadata.resolve_task_context(case.db, case.task.external_task_key, None))


def test_failed_read_preserves_last_good_values_and_retries(case, monkeypatch):
    record = refresh(case)
    before = deepcopy(record.fields_json)
    revision, checked = record.revision, record.checked_at
    metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    def fail(*args): raise HTTPException(status_code=502, detail="upstream inaccessible")
    monkeypatch.setattr(metadata, "fetch_monday_metadata", fail)
    assert metadata.run_metadata_once(case.db, "token") == ["failed"]
    record = case.db.get(TaskMondayMetadata, case.task.external_task_key)
    assert record.fields_json == before and record.revision == revision and record.checked_at == checked
    assert record.last_error and record.requested_generation > record.completed_generation
    assert metadata.aware(record.scheduled_for) > metadata.utc_now()


def test_new_event_during_fetch_cannot_publish_superseded_result(case, monkeypatch):
    original_revision = refresh(case).revision
    metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    claim = metadata.claim_refresh(case.db, account_id="acct")
    def fetch(*args):
        old = deepcopy(case.item)
        metadata.enqueue_metadata(case.db, case.task)
        case.db.commit()
        return old
    monkeypatch.setattr(metadata, "fetch_monday_metadata", fetch)
    assert metadata.execute_refresh(case.db, claim, "token") == "superseded"
    record = case.db.get(TaskMondayMetadata, case.task.external_task_key)
    assert record.revision == original_revision
    assert record.requested_generation > record.completed_generation
    assert record.lease_token is None


def test_expired_lease_cannot_overwrite_new_worker(case):
    metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    old_claim = metadata.claim_refresh(case.db, account_id="acct")
    record = case.db.get(TaskMondayMetadata, case.task.external_task_key)
    record.lease_until = metadata.utc_now() - timedelta(seconds=1)
    case.db.commit()
    new_claim = metadata.claim_refresh(case.db, account_id="acct")
    assert old_claim[1] != new_claim[1]
    assert metadata.execute_refresh(case.db, new_claim, "token") == "completed"
    assert metadata.execute_refresh(case.db, old_claim, "token") == "lease_lost"


def test_actual_group_is_checked_before_publication(case):
    metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    case.item["group"]["id"] = "group_mkpbd6vy"
    assert metadata.run_metadata_once(case.db, "token") == ["ineligible"]
    assert case.db.get(TaskMondayMetadata, case.task.external_task_key).revision is None


def test_cancel_invalidates_inflight_lease(case):
    metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    claim = metadata.claim_refresh(case.db, account_id="acct")
    metadata.cancel_metadata(case.db, case.task)
    case.db.commit()
    assert metadata.execute_refresh(case.db, claim, "token") == "lease_lost"


def test_task_disabled_while_read_is_in_flight_cannot_publish(case, monkeypatch):
    metadata.enqueue_metadata(case.db, case.task, immediate=True)
    case.db.commit()
    claim = metadata.claim_refresh(case.db, account_id="acct")
    def fetch(*args):
        response = deepcopy(case.item)
        case.task.auto_sync_state = "excluded"
        case.db.commit()
        return response
    monkeypatch.setattr(metadata, "fetch_monday_metadata", fetch)
    assert metadata.execute_refresh(case.db, claim, "token") == "ineligible"
    assert case.db.get(TaskMondayMetadata, case.task.external_task_key).revision is None


def test_linked_board_webhook_enqueues_active_dependents_without_source_item_ingestion(case):
    refresh(case)
    event = monday_webhooks.normalize_webhook_payload({"event": {
        "boardId": "1825117125", "pulseId": "601", "columnId": "text3__1", "type": "change_column_value",
    }})
    dispatch = SimpleNamespace()
    monday_webhooks._dispatch_auto_sync(case.db, dispatch, item=None, normalized=event)
    assert dispatch.outcome == "queued" and dispatch.result_json["dependentTasksQueued"] == 1
    assert case.db.query(AutoSyncJob).count() == 0
    case.task.auto_sync_state = "completed_retained"
    case.db.commit()
    assert metadata.enqueue_linked_dependents(case.db, "1825117125", "601") == 0


def test_relink_updates_reverse_dependency_index(case):
    refresh(case)
    case.item["column_values"][2]["linked_item_ids"] = ["602"]
    case.item["column_values"][2]["linked_items"][0]["id"] = "602"
    case.item["column_values"][3]["mirrored_items"][0]["linked_item"]["id"] = "602"
    refresh(case)
    assert metadata.enqueue_linked_dependents(case.db, "1825117125", "601") == 0
    assert metadata.enqueue_linked_dependents(case.db, "1825117125", "602") == 1


def test_ui_chat_and_download_use_same_current_values_preserving_email_metadata(case, monkeypatch):
    snapshot_context = deepcopy(case.item)
    snapshot_context["csv_params"] = [{"records": [{"parameter": "Company", "value": "Email Sender Ltd"}]}]
    case.db.add(TaskSnapshot(id=uuid.uuid4(), external_task_key=case.task.external_task_key,
        snapshot_version="old", task_context_json=snapshot_context, ingestion_status="complete"))
    case.item["column_values"][3]["mirrored_items"][0]["mirrored_value"]["text"] = "Updated project"
    refresh(case)
    monkeypatch.setattr(tasks, "require_task_access", lambda *args: case.task)
    summary = tasks.task_summary(case.task.external_task_key, db=case.db, current_user=None)
    context = retrieval.get_task_context(case.db, case.task.external_task_key)
    assert context == summary.taskContext
    assert context["csv_params"] == snapshot_context["csv_params"]
    assert next(col for col in context["column_values"] if col["id"] == FIELDS[3][1])["text"] == "Updated project"
    assert "Airport, South Terminal" not in str(context)
    download = tasks.monday_columns(case.task.external_task_key, db=case.db, current_user=None)
    assert "Updated project" in download.body.decode()
    assert download.headers["cache-control"] == "no-store"


def test_metadata_only_edit_queues_no_document_job(case):
    original = deepcopy(case.item)
    revision = compute_desired_source_revision(original)
    case.db.add(TaskSnapshot(id=uuid.uuid4(), external_task_key=case.task.external_task_key,
        snapshot_version=revision, task_context_json=original, ingestion_status="complete"))
    case.task.latest_snapshot_version = case.task.last_indexed_source_revision = revision
    case.db.commit()
    case.item["column_values"][4]["values"] = [{"id": "87", "label": "RG"}]
    result = apply_auto_sync_policy_for_item(case.db, case.item, trigger_type="webhook",
        desired_source_revision=compute_desired_source_revision(case.item))
    assert result.metadata_queued and result.job is None
    assert case.db.query(AutoSyncJob).count() == 0
    changed_attachment = deepcopy(case.item)
    changed_attachment["assets"] = [{"id": "new", "name": "roof.pdf"}]
    assert not only_crm_fields_changed(original, changed_attachment)
    assert not only_crm_fields_changed(original, {**original, "updated_at": "later"})


def test_reconciliation_checks_metadata_even_when_parent_timestamp_is_unchanged(case, monkeypatch):
    revision = compute_desired_source_revision(case.item)
    case.task.latest_snapshot_version = case.task.last_indexed_source_revision = revision
    case.db.add(TaskSnapshot(id=uuid.uuid4(), external_task_key=case.task.external_task_key,
        snapshot_version=revision, task_context_json=deepcopy(case.item), ingestion_status="complete"))
    case.db.commit()
    monkeypatch.setattr(auto_sync_reconciliation, "fetch_current_account_id", lambda token: "acct")
    monkeypatch.setattr(auto_sync_reconciliation, "list_item_ids_in_groups", lambda *args, **kw: {"topics": ["123"]})
    monkeypatch.setattr(auto_sync_reconciliation, "fetch_current_source_revision_inputs", lambda *args, **kw: deepcopy(case.item))
    result = auto_sync_reconciliation.reconcile_active_items_once(case.db, dry_run=False, access_token="token", policy=policy_from_settings())
    assert result.queued == 0
    assert case.db.get(TaskMondayMetadata, case.task.external_task_key).requested_generation == 1


def test_metadata_query_contains_no_assets_and_uses_typed_columns(monkeypatch):
    seen = []
    def request(token, query, variables, **kwargs):
        seen.append((query, variables))
        return {"data": {"items": [monday_item()]}}
    monkeypatch.setattr(monday_client, "monday_graphql_request", request)
    assert monday_client.fetch_monday_metadata("token", "123")["id"] == "123"
    query, variables = seen[0]
    assert "assets" not in query and "BoardRelationValue" in query and "mirrored_items" in query
    assert set(variables["columnIds"]) == {field[1] for field in FIELDS}


def test_idle_worker_makes_no_monday_calls(case, monkeypatch):
    monkeypatch.setattr(metadata, "fetch_current_account_id", lambda token: pytest.fail("idle worker must not call Monday"))
    assert metadata.run_metadata_once(case.db, "token") == []


def test_historical_columns_are_excluded_from_retrieval_only_after_current_metadata_exists(case, monkeypatch):
    from pgvector.sqlalchemy import Vector
    # SQLite has no vector distance operator. Use a constant score, leaving the
    # actual production joins, snapshot scoping and evidence filter intact.
    monkeypatch.setattr(Vector.comparator_factory, "cosine_distance", lambda self, vector: literal(0.5))
    snapshot = TaskSnapshot(id=uuid.uuid4(), external_task_key=case.task.external_task_key,
        snapshot_version="old", task_context_json={}, ingestion_status="complete")
    case.db.add(snapshot)
    for kind in ("monday_columns", "email"):
        file = TaskFile(id=uuid.uuid4(), external_task_key=case.task.external_task_key,
            snapshot_id=snapshot.id, kind=kind, original_filename=kind, bucket="test", object_path=kind)
        case.db.add(file)
        case.db.add(TaskChunk(id=uuid.uuid4(), file_id=file.id, chunk_text=f"{kind} evidence", embedding=[1.0] * 1536))
    case.db.commit()
    def evidence():
        return retrieval._search_snapshot_for_embedding(case.db, case.task.external_task_key, snapshot.id, "test", 0, [1.0] * 1536, 8)
    assert {chunk["filename"] for chunk in evidence()} == {"email", "monday_columns"}
    refresh(case)
    assert {chunk["filename"] for chunk in evidence()} == {"email"}
    monkeypatch.setattr(tasks, "require_task_access", lambda *args: case.task)
    assert [file.kind for file in tasks.task_sources(case.task.external_task_key, db=case.db, current_user=None).files] == ["email"]


def test_metadata_migration_round_trip_preserves_existing_tasks(monkeypatch):
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    migration = importlib.import_module("backend.migrations.versions.0015_monday_metadata")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        Task.__table__.create(connection)
        connection.execute(Task.__table__.insert().values(
            external_task_key="existing", account_id="acct", board_id="board", item_id="item"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert {"task_monday_metadata", "monday_metadata_links"}.issubset(inspect(connection).get_table_names())
        assert {col["name"] for col in inspect(connection).get_columns("task_monday_metadata")} == set(TaskMondayMetadata.__table__.columns.keys())
        migration.downgrade()
        assert inspect(connection).get_table_names() == ["tasks"]
        assert connection.execute(Task.__table__.select()).one().external_task_key == "existing"
    engine.dispose()


def test_snapshot_selection_breaks_creation_timestamp_ties_by_completion(case):
    created = datetime(2026, 9, 24, tzinfo=timezone.utc)
    snapshots = [TaskSnapshot(id=uuid.uuid4(), external_task_key=case.task.external_task_key,
        snapshot_version=f"rev-{i}", task_context_json={}, ingestion_status="complete",
        created_at=created, completed_at=created + timedelta(microseconds=i)) for i in (1, 2)]
    case.db.add_all(snapshots)
    case.db.commit()
    assert retrieval._latest_snapshot(case.db, case.task.external_task_key).snapshot_version == "rev-2"
