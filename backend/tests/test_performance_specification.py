from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import io
import json
import shutil
import subprocess
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import (
    DesignProcessingItem, PerformanceSpecification, PerformanceSpecificationArtifact,
    PerformanceSpecificationLink, Task, MondayWebhookEvent,
)
from backend.app.monday_client import MondayFileColumnAsset
from backend.app.services import performance_specification as worker
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.design_processing_inputs import DesignEmailAsset, DesignProcessingTargetSnapshot
from backend.app.services.performance_specification_contract import (
    CADTranslation, FIELD_NAMES, OPTIONS, pdf_values, translate_specification,
)
from backend.app.services.performance_specification_pdf import render_performance_specification_pdf
from backend.app.services.performance_specification_sources import (
    SpecificationSourceError, active_specification_group_ids, resolve_specification_sources,
)
from backend.app.services.sync_pipeline import _is_ai_data_pdf_preview


BOARD = "1882196103"
ITEM = "123"
PROJECT_BOARD = "1825117125"


def column(column_id, title, value, kind="text"):
    result = {"id": column_id, "column": {"title": title}, "type": kind,
              "text": value, "value": None}
    if kind == "date":
        result["date"] = value
    return result


def relation(column_id, values):
    return {"id": column_id, "type": "board_relation", "column": {"title": column_id},
            "linked_item_ids": [entry["id"] for entry in values], "linked_items": values}


def translation_payload(raw):
    aliases = {"1 : 100": "1:100", "A1": "ISO A1", "TR27": "Tissue Faced PIR"}
    values = {label: raw.get(label) for label in FIELD_NAMES}
    values["Incoming Information"] = ""
    for label in OPTIONS:
        values[label] = aliases.get(values[label], values[label])
    for label in ("Target U Value", "Target Min U Value"):
        if values[label] is not None:
            values[label] = str(Decimal(values[label]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {"values": values, "warnings": []}


class Storage:
    def __init__(self):
        self.objects = {}

    def write_private(self, bucket, key, content, content_type):
        assert content_type == "application/pdf"
        self.objects[bucket, key] = content

    def read_private(self, bucket, key):
        return self.objects[bucket, key]


class Gateway:
    def __init__(self):
        self.target = SimpleNamespace(board_id=BOARD, item_id=ITEM, group_id="topics",
                                      item_state="active", input_revision="email-v1")
        self.holding = {
            "id": ITEM, "board": {"id": BOARD}, "state": "active", "group": {"id": "topics"},
            "column_values": [
                relation("board_relation_mkpbm5np", [{"id": "901", "name": "00901", "board": {"id": PROJECT_BOARD}}]),
                relation("board_relation_mm3c4g5x", [{"id": "801", "name": "Acme, Limited", "board": {"id": "1654217230"}}]),
                column("date_mkpb23av", "Date Received", "2026-10-30", "date"),
            ],
        }
        self.project = {
            "id": "901", "board": {"id": PROJECT_BOARD}, "state": "active",
            "column_values": [column("text3__1", "Project Name", "Warehouse South")],
            "subitems": [{
                "id": "9101", "name": "TP00901_26.01 - A", "state": "active", "board": {"id": "9000"},
                "column_values": [column("mirror0__1", "U-Value", "0.2"),
                                  column("mirror875__1", "Product Type", "TR27")],
            }],
        }
        self.files = [MondayFileColumnAsset("77", "Existing.csv", 10, None),
                      MondayFileColumnAsset("78", "AI_Data_Preview_original.pdf", 20, None)]
        self.uploads, self.deletes = [], []
        self.on_upload = None

    def fetch_target(self, item_id):
        return deepcopy(self.target)

    def fetch_holding(self, item_id):
        return deepcopy(self.holding)

    def fetch_project(self, item_id):
        return deepcopy(self.project)

    def inspect_files(self, item_id):
        return tuple(self.files)

    def upload(self, item_id, filename, content):
        asset = MondayFileColumnAsset(str(100 + len(self.uploads)), filename, len(content), None)
        self.files.append(asset)
        self.uploads.append((asset, content))
        if self.on_upload:
            self.on_upload()
        return asset

    def delete(self, board_id, item_id, asset_id):
        self.deletes.append(asset_id)
        self.files = [asset for asset in self.files if asset.asset_id != asset_id]


@pytest.fixture()
def case(monkeypatch):
    monkeypatch.setattr(settings, "performance_specification_enabled", True)
    monkeypatch.setattr(settings, "design_processing_mode", "enabled")
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics,group_mkpbd6vy,group_mkpbb3tx")
    monkeypatch.setattr(settings, "performance_specification_column_map", {})
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        raw = {"Scale": "1 : 100", "Page Size": "A1", "Drawing Reference": "TP00901_26.01 - A",
               "Drawing Title": "Old title", "Date Received": "29 Oct 2026", "Company": "Email company",
               "Contact": "Alex Example", "Target U-Value": "0.18", "Target Min U-Value": None,
               "Tapered Insulation": "TR27", "Decking": "Concrete", "Bauder Roof Num": "007"}
        item = DesignProcessingItem(
            id=uuid.uuid4(), board_id=BOARD, item_id=ITEM, state="ready_for_review", warnings_json=[],
            latest_desired_input_revision="email-v1", latest_analyzed_input_revision="email-v1",
            latest_desired_pipeline_version="extraction-v1", latest_analyzed_pipeline_version="extraction-v1",
            extracted_parameters_json={"inputRevision": "email-v1", "pipelineVersion": "extraction-v1",
                                       "performanceParameters": raw, "parameters": raw},
        )
        db.add(item)
        db.commit()
        calls = []
        on_translate = []

        def generate(model, prompt, config):
            calls.append((model, prompt, config))
            if on_translate:
                on_translate.pop(0)()
            values = json.loads(prompt.split("\nResolved values: ", 1)[1])
            return SimpleNamespace(parsed=translation_payload(values), text=None)

        yield SimpleNamespace(db=db, item=item, gateway=Gateway(), storage=Storage(), calls=calls,
                              on_translate=on_translate,
                              client=LegacyGeminiClient("gemini-3.8-flash", generate_content=generate))
    engine.dispose()


def run(case):
    worker.enqueue_specification(case.db, BOARD, ITEM, group_id=case.gateway.target.group_id, immediate=True)
    case.db.commit()
    return worker.run_specifications_once(case.db, gateway=case.gateway, client=case.client, storage=case.storage)


def current(case):
    return case.db.get(PerformanceSpecification, (BOARD, ITEM), populate_existing=True)


def test_resolves_current_crm_and_keeps_optional_roof_number_and_raw_insulation(case):
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["raw"]["Drawing Title"] == "Warehouse South"
    assert source["raw"]["Company"] == "Acme, Limited"
    assert source["raw"]["Target U Value"] == "0.2"
    assert source["raw"]["Bauder Roof Num"] == "007"
    assert source["raw"]["Tapered Insulation"] == "TR27"
    assert source["raw"]["Incoming Information"] == ""
    assert ["9000", "9101"] in source["dependencies"]


def test_llm_translation_is_structured_and_strict(case):
    source = resolve_specification_sources(case.gateway, case.item)
    translation = translate_specification(source["raw"], case.client)
    values = pdf_values(translation)
    assert values["Page Size"] == "ISO A1"
    assert values["Target U Value"] == "0.20"
    assert values["Target Min U Value"] == "Not Provided"
    assert values["Incoming Information"] == ""
    assert values["Bauder Roof Num"] == "007"
    assert case.calls[0][2].response_mime_type == "application/json"
    assert case.calls[0][2].temperature is None


@pytest.mark.parametrize("label,value", [
    ("Scale", "1:200"), ("Page Size", "A1"), ("Decking", "Timber"),
    ("Tapered Insulation", "EPS"), ("Target U Value", "0.2"),
    ("Target Min U Value", "NaN"), ("Incoming Information", "anything"),
])
def test_rejects_values_outside_cad_contract(label, value):
    payload = translation_payload({})
    payload["values"][label] = value
    with pytest.raises(ValidationError):
        CADTranslation.model_validate(payload)


def test_model_cannot_invent_missing_values_change_identifiers_or_numbers(case):
    raw = resolve_specification_sources(case.gateway, case.item)["raw"]
    for label, invented in [("Bauder Roof Num", "7"), ("Target U Value", "0.18"), ("Surveyor", "Invented")]:
        payload = translation_payload(raw)
        payload["values"][label] = invented
        client = LegacyGeminiClient("gemini-3.8-flash", generate_content=lambda *args: SimpleNamespace(parsed=payload))
        with pytest.raises(ValueError):
            translate_specification(raw, client)


def test_model_cannot_replace_an_unsupported_scale_with_a_nearest_allowed_scale(case):
    raw = resolve_specification_sources(case.gateway, case.item)["raw"]
    raw["Scale"] = "1:200"
    payload = translation_payload(raw)
    payload["values"]["Scale"] = "1:250"
    client = LegacyGeminiClient("gemini-3.8-flash", generate_content=lambda *args: SimpleNamespace(parsed=payload))
    with pytest.raises(ValueError, match="specified scale"):
        translate_specification(raw, client)


@pytest.mark.parametrize("unit", ["W/m2K", "W/m²K", "W/(m^2.K)"])
def test_u_value_validation_distinguishes_units_from_additional_numbers(case, unit):
    raw = resolve_specification_sources(case.gateway, case.item)["raw"]
    payload = translation_payload(raw)
    raw["Target U Value"] = f"0.20 {unit}"
    client = LegacyGeminiClient("gemini-3.8-flash", generate_content=lambda *args: SimpleNamespace(parsed=payload))
    assert pdf_values(translate_specification(raw, client))["Target U Value"] == "0.20"


@pytest.mark.parametrize("group", ["group_mkpbd6vy", "group_mkpbb3tx", "other"])
def test_excluded_groups_never_translate_or_upload_even_if_configured_active(case, group):
    case.gateway.target.group_id = group
    assert run(case) == []
    assert not case.calls and not case.gateway.uploads
    assert group not in active_specification_group_ids()


def test_move_to_excluded_group_cancels_running_claim(case):
    worker.enqueue_specification(case.db, BOARD, ITEM, group_id="topics", immediate=True)
    case.db.commit()
    claim = worker.claim_specification(case.db)
    case.gateway.target.group_id = "group_mkpbd6vy"
    assert worker.execute_specification(case.db, claim, gateway=case.gateway,
                                       client=case.client, storage=case.storage) == "excluded"
    assert not case.calls and not case.gateway.uploads


def test_publishes_pdf_once_reuses_translation_and_preserves_other_files(case):
    assert run(case) == ["published"]
    assert len(case.calls) == len(case.gateway.uploads) == 1
    revision = current(case).current_revision
    assert run(case) == ["unchanged"]
    assert current(case).current_revision == revision
    assert len(case.calls) == len(case.gateway.uploads) == 1
    assert {asset.filename for asset in case.gateway.files} >= {"Existing.csv", "AI_Data_Preview_original.pdf"}
    assert case.db.query(PerformanceSpecificationLink).count() == 3


def test_crm_change_refreshes_without_extraction_and_replaces_only_prior_specification(case):
    assert run(case) == ["published"]
    original = current(case).current_revision
    first_asset = case.gateway.uploads[0][0].asset_id
    extraction_before = deepcopy(case.item.extracted_parameters_json)
    case.gateway.project["subitems"][0]["column_values"][0]["text"] = "0.15"
    assert run(case) == ["published"]
    assert current(case).current_revision != original
    assert case.item.extracted_parameters_json == extraction_before
    assert case.gateway.deletes == [first_asset]
    assert len(case.gateway.files) == 3


def test_confirmed_clear_does_not_resurrect_email_value(case):
    assert run(case) == ["published"]
    case.gateway.project["subitems"][0]["column_values"][0]["text"] = None
    assert run(case) == ["published"]
    record = current(case)
    assert record.source_json["raw"]["Target U Value"] is None
    assert record.source_json["sources"]["Target U Value"]["cleared"] is True
    artifact = case.db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, record.current_revision))
    assert artifact.translation_json["values"]["Target U Value"] is None
    assert run(case) == ["unchanged"]


def test_read_failure_preserves_last_successful_pdf_and_retries(case):
    assert run(case) == ["published"]
    revision = current(case).current_revision
    case.gateway.holding["column_values"].pop()
    assert run(case) == ["failed"]
    assert current(case).current_revision == revision
    assert current(case).last_error
    assert current(case).scheduled_for
    assert not case.gateway.deletes


def test_unavailable_or_ambiguous_linked_project_is_not_silently_merged(case):
    extra = deepcopy(case.gateway.project["subitems"][0])
    extra.update(id="9102", name="TP00901_26.02 - A")
    case.gateway.project["subitems"].append(extra)
    case.item.extracted_parameters_json["performanceParameters"]["Drawing Reference"] = None
    with pytest.raises(SpecificationSourceError, match="ambiguous"):
        resolve_specification_sources(case.gateway, case.item)


def test_schema_valid_translation_cannot_publish_after_sources_change(case):
    case.on_translate.append(lambda: case.gateway.project["column_values"][0].update(text="Changed title"))
    assert run(case) == ["superseded"]
    assert not case.gateway.uploads
    assert current(case).current_revision is None
    assert run(case) == ["published"]


def test_group_move_during_translation_prevents_publication(case):
    case.on_translate.append(lambda: setattr(case.gateway.target, "group_id", "group_mkpbb3tx"))
    assert run(case) == ["excluded"]
    assert not case.gateway.uploads


def test_notification_during_translation_supersedes_generation(case):
    case.on_translate.append(lambda: worker.enqueue_specification(case.db, BOARD, ITEM, group_id="topics"))
    assert run(case) == ["superseded"]
    assert not case.gateway.uploads


def test_upload_timeout_recovers_by_adoption_without_duplicate_upload(case):
    def timeout():
        case.gateway.on_upload = None
        raise TimeoutError("lost upload response")
    case.gateway.on_upload = timeout
    assert run(case) == ["failed"]
    assert run(case) == ["unchanged"]
    assert len(case.gateway.uploads) == len(case.calls) == 1


def test_change_during_upload_never_promotes_stale_result_and_cleans_it_after_replacement(case):
    def change():
        case.gateway.on_upload = None
        case.gateway.project["column_values"][0]["text"] = "New project title"
    case.gateway.on_upload = change
    assert run(case) == ["superseded"]
    assert current(case).current_revision is None
    assert run(case) == ["published"]
    assert len(case.gateway.uploads) == 2
    assert case.gateway.deletes == [case.gateway.uploads[0][0].asset_id]


def test_shadow_mode_renders_without_monday_mutations_then_publishes_cached_result(case, monkeypatch):
    monkeypatch.setattr(settings, "design_processing_mode", "shadow")
    assert run(case) == ["rendered"]
    assert not case.gateway.uploads
    assert current(case).current_revision is None
    monkeypatch.setattr(settings, "design_processing_mode", "enabled")
    assert run(case) == ["published"]
    assert len(case.calls) == 1


def test_expired_worker_is_fenced_from_publication(case):
    worker.enqueue_specification(case.db, BOARD, ITEM, immediate=True)
    case.db.commit()
    old = worker.claim_specification(case.db)
    current(case).lease_until = worker.utc_now() - timedelta(seconds=1)
    case.db.commit()
    newer = worker.claim_specification(case.db)
    assert old[2] != newer[2]
    assert worker.execute_specification(case.db, old, gateway=case.gateway,
                                       client=case.client, storage=case.storage) == "superseded"
    assert not case.gateway.uploads


def test_corrupt_stored_pdf_is_not_uploaded(case):
    assert run(case) == ["published"]
    key = next(iter(case.storage.objects))
    case.storage.objects[key] = b"corrupted"
    assert run(case) == ["failed"]
    assert len(case.gateway.uploads) == 1


def test_duplicate_monday_candidates_require_review(case):
    assert run(case) == ["published"]
    existing = case.gateway.files[-1]
    case.gateway.files.append(MondayFileColumnAsset("999", existing.filename, existing.size_bytes, None))
    assert run(case) == ["failed"]
    assert not case.gateway.deletes


def test_linked_changes_enqueue_dependents(case):
    assert run(case) == ["published"]
    generation = current(case).requested_generation
    assert worker.enqueue_specification_dependents(case.db, "9000", "9101") == 1
    case.db.commit()
    assert current(case).requested_generation == generation + 1


def test_missing_roof_number_is_optional_and_incoming_information_is_always_blank(case):
    case.item.extracted_parameters_json["performanceParameters"]["Bauder Roof Num"] = None
    source = resolve_specification_sources(case.gateway, case.item)
    values = pdf_values(translate_specification(source["raw"], case.client))
    assert values["Bauder Roof Num"] == "Not Provided"
    assert values["Incoming Information"] == ""
    schema = DesignParameterExtraction.model_json_schema()
    assert "bauder_roof_num" in schema["properties"]


def test_generated_specification_is_excluded_from_source_ingestion():
    assert _is_ai_data_pdf_preview({"name": "Project_Performance_Specification_Details_123_hash.pdf"}, "csv")
    assert not _is_ai_data_pdf_preview({"name": "Customer_specification.pdf"}, "csv")


def test_pdf_text_contains_exact_ordered_contract_and_handles_long_unicode_values(case):
    raw = resolve_specification_sources(case.gateway, case.item)["raw"]
    raw["Contact"] = "Zoë Müller"
    raw["Drawing Title"] = ("Warehouse & plant <north> " * 50).strip()
    raw["Company"] = ("Example roofing company, " * 70).strip()
    translation = translate_specification(raw, case.client)
    content = render_performance_specification_pdf(translation, item_id=ITEM, revision="abc123")
    if shutil.which("pdftotext"):
        result = subprocess.run(["pdftotext", "-", "-"], input=content, capture_output=True, check=True)
        text = result.stdout.decode("utf-8")
        page_count = text.count("\f")
    else:
        pypdf = pytest.importorskip("pypdf")
        reader = pypdf.PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() for page in reader.pages)
        page_count = len(reader.pages)
    positions = [text.index(label + ":") for label in FIELD_NAMES]
    assert positions == sorted(positions)
    assert "Zoë Müller" in text
    assert "Target U Value: 0.20" in text
    assert "Target Min U Value: Not Provided" in text
    assert "Incoming Information: Not Provided" not in text
    assert page_count >= 2


def test_typed_mirror_numbers_preserve_underlying_source_identity(case):
    case.gateway.project["subitems"][0]["column_values"][0] = {
        "id": "mirror0__1", "type": "mirror", "column": {"title": "U-Value"},
        "display_value": "0.175", "mirrored_items": [{
            "linked_item": {"id": "777", "board": {"id": "888"}},
            "mirrored_value": {"__typename": "NumbersValue", "number": 0.175},
        }],
    }
    source = resolve_specification_sources(case.gateway, case.item)
    assert ["888", "777"] in source["dependencies"]
    values = pdf_values(translate_specification(source["raw"], case.client))
    assert values["Target U Value"] == "0.18"


def test_unresolved_mirror_is_a_failed_read_and_not_a_clear(case):
    case.gateway.project["subitems"][0]["column_values"][0] = {
        "id": "mirror0__1", "type": "mirror", "column": {"title": "U-Value"},
        "display_value": "0.20", "mirrored_items": [],
    }
    with pytest.raises(SpecificationSourceError, match="not resolved"):
        resolve_specification_sources(case.gateway, case.item)


def test_explicit_mapping_missing_column_stops_refresh(case, monkeypatch):
    monkeypatch.setattr(settings, "performance_specification_column_map", {"drawing": {"VCL": "missing"}})
    assert run(case) == ["failed"]
    assert "Configured drawing column" in current(case).last_error
    assert not case.gateway.uploads


def test_unique_newer_letter_is_selected_without_switching_roof(case):
    case.gateway.project["subitems"][0]["name"] = "TP00901_26.01 - B"
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["raw"]["Drawing Reference"] == "TP00901_26.01 - B"
    case.gateway.project["subitems"][0]["name"] = "TP00901_26.02 - A"
    with pytest.raises(SpecificationSourceError, match="missing or ambiguous"):
        resolve_specification_sources(case.gateway, case.item)


def test_old_extraction_waits_for_new_roof_aware_schema(case):
    old = deepcopy(case.item.extracted_parameters_json)
    old.pop("performanceParameters")
    case.item.extracted_parameters_json = old
    case.db.commit()
    assert run(case) == ["failed"]
    assert "output-v8" in current(case).last_error
    assert not case.calls


def test_monday_metadata_refresh_and_cancellation_drive_specification_queue(case):
    from backend.app.services import monday_metadata
    task = Task(external_task_key=f"acct:{BOARD}:{ITEM}", account_id="acct", board_id=BOARD,
                item_id=ITEM, auto_sync_state="active", auto_sync_enabled=True,
                source_group_id="topics", sync_status="completed")
    case.db.add(task)
    case.db.commit()
    monday_metadata.enqueue_metadata(case.db, task, immediate=True)
    case.db.commit()
    assert current(case).requested_generation == 1
    monday_metadata.cancel_metadata(case.db, task)
    case.db.commit()
    assert current(case).scheduled_for is None
    assert current(case).completed_generation == current(case).requested_generation


def test_existing_active_projects_bootstrap_without_duplicate_queue_records(case):
    task = Task(external_task_key=f"acct:{BOARD}:{ITEM}", account_id="acct", board_id=BOARD,
                item_id=ITEM, auto_sync_state="active", auto_sync_enabled=True,
                source_group_id="topics", sync_status="completed")
    case.db.add(task)
    case.db.commit()
    assert worker.discover_specifications(case.db) == 1
    assert worker.discover_specifications(case.db) == 0
    assert current(case).requested_generation == 1


def test_subitem_webhook_queues_parent_dependency_and_ignores_output_files(case):
    from backend.app.routes import monday_webhooks
    assert run(case) == ["published"]
    generation = current(case).requested_generation
    event = MondayWebhookEvent(
        id=uuid.uuid4(), idempotency_key="new-subitem", status="processing",
        payload_json={"event": {"parentItemId": "901", "parentItemBoardId": PROJECT_BOARD}},
    )
    case.db.add(event)
    case.db.commit()
    dispatch = SimpleNamespace(webhook_event_id=event.id)
    normalized = monday_webhooks.normalize_webhook_payload({"event": {
        "boardId": "9000", "pulseId": "9102", "type": "create_subitem",
    }})
    monday_webhooks._dispatch_auto_sync(case.db, dispatch, item=None, normalized=normalized)
    case.db.commit()
    assert current(case).requested_generation == generation + 1
    normalized = monday_webhooks.normalize_webhook_payload({"event": {
        "boardId": "9000", "pulseId": "9101", "type": "change_column_value", "columnId": "file_mkza7y37",
    }})
    event.payload_json = {}
    case.db.commit()
    monday_webhooks._dispatch_auto_sync(case.db, dispatch, item=None, normalized=normalized)
    case.db.commit()
    assert current(case).requested_generation == generation + 1


def test_migration_upgrade_and_downgrade_build_independent_tables():
    from importlib import import_module
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    migration = import_module("backend.migrations.versions.0017_performance_specification")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert set(inspect(connection).get_table_names()) == {
                "performance_specifications", "performance_specification_links", "performance_specification_artifacts",
            }
            migration.downgrade()
            assert inspect(connection).get_table_names() == []
    engine.dispose()


@pytest.mark.parametrize("dry_run", [True, False])
def test_design_reconciliation_recovers_specification_without_repeating_analysis(case, dry_run):
    from backend.app.services.design_processing_reconciliation import reconcile_landing_zone_once
    version = settings.design_processing_pipeline_version
    case.item.latest_analyzed_pipeline_version = case.item.latest_desired_pipeline_version = version
    case.item.latest_published_input_revision = "email-v1"
    case.item.latest_published_pipeline_version = version
    worker.enqueue_specification(case.db, BOARD, ITEM, group_id="topics", immediate=True)
    worker.cancel_specification(case.db, BOARD, ITEM)
    case.db.commit()
    generation = current(case).requested_generation
    snapshot = DesignProcessingTargetSnapshot(
        board_id=BOARD, item_id=ITEM, group_id="topics", item_state="active", name="Project",
        input_revision="email-v1", email_assets=(DesignEmailAsset(
            asset_id="1", filename="request.eml", file_extension="eml", size=1,
            created_at="2026-10-30T12:00:00Z", download_url="unused", download_requires_auth=False,
        ),),
    )
    result = reconcile_landing_zone_once(
        case.db, item_id=ITEM, dry_run=dry_run, access_token="token", mode="enabled",
        gateway=SimpleNamespace(fetch_target=lambda _: snapshot),
    )
    assert result.errors == 0
    assert result.queued == 0
    assert current(case).requested_generation == generation + (0 if dry_run else 1)
    assert (current(case).scheduled_for is None) is dry_run
    assert not case.calls


def test_cleanup_failure_retries_without_retranslating_or_reuploading(case):
    assert run(case) == ["published"]
    original_delete = case.gateway.delete

    def fail_once(*args):
        case.gateway.delete = original_delete
        raise TimeoutError("delete failed")

    case.gateway.delete = fail_once
    case.gateway.project["column_values"][0]["text"] = "Changed title"
    assert run(case) == ["published"]
    assert current(case).last_error.startswith("Specification cleanup:")
    assert len(case.gateway.files) == 4
    assert run(case) == ["unchanged"]
    assert current(case).last_error is None
    assert len(case.gateway.files) == 3
    assert len(case.calls) == len(case.gateway.uploads) == 2
