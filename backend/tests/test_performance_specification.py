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
    CADTranslation, FIELD_NAMES, OPTIONS, SpecificationPublication, pdf_values, translate_specification,
)
from backend.app.services.performance_specification_pdf import render_performance_specification_pdf
from backend.app.services.performance_specification_sources import (
    SpecificationSourceError, active_specification_group_ids, resolve_specification_sources,
)
from backend.app.services.specification_semantics import EVIDENCE_VERSION, canonical_label
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


def with_evidence(extraction):
    facts = {canonical_label(label): value for label, value in extraction["performanceParameters"].items() if value is not None}
    text = "\n".join(f"{label}: {value}" for label, value in facts.items())
    return {**extraction, "specificationEvidenceVersion": EVIDENCE_VERSION,
            "specificationAssessedFields": list(FIELD_NAMES),
            "sourceManifest": [{"sourceId": "email:body", "filename": "request.eml", "sourceType": "email_body",
                                "contentSha256": "abc", "status": "read", "text": text, "observedAt": "2026-10-29T12:00:00+00:00"}],
            "specificationEvidence": [{"field": label, "value": value, "source_id": "email:body",
                                       "snippet": f"{label}: {value}", "location": "email body", "scope": None,
                                       "issuer": "TaperedPlus" if label == "Drawing Reference" else None,
                                       "role": "requested", "request_role": "direct_request", "rationale": "Explicit request"} for label, value in facts.items()]}


def pdf_text(content):
    if shutil.which("pdftotext"):
        result = subprocess.run(["pdftotext", "-", "-"], input=content, capture_output=True, check=True)
        return " ".join(result.stdout.decode("utf-8").split())
    pypdf = pytest.importorskip("pypdf")
    return " ".join(" ".join(page.extract_text() for page in pypdf.PdfReader(io.BytesIO(content)).pages).split())


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
            latest_desired_pipeline_version=settings.design_processing_pipeline_version,
            latest_analyzed_pipeline_version=settings.design_processing_pipeline_version,
            extracted_parameters_json=with_evidence({"inputRevision": "email-v1", "pipelineVersion": settings.design_processing_pipeline_version,
                                       "performanceParameters": raw, "parameters": raw}),
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


@pytest.fixture()
def contact_case(case):
    drawing = case.gateway.project["subitems"][0]
    contact = {
        "id": "mirror_11__1", "type": "mirror", "column": {"title": "Contact"},
        "display_value": "Example, Sam", "mirrored_items": [{
            "linked_item": {"id": "777", "board": {"id": "888"}},
            "mirrored_value": {"__typename": "BoardRelationValue", **relation("contact", [
                {"id": "555", "name": "Example, Sam", "board": {"id": "666"}},
            ])},
        }],
    }
    drawing["column_values"].append(contact)
    other = deepcopy(drawing)
    other.update(id="9102", name="TP00901_26.02 - A")
    # An unreadable Contact on a different drawing must never be consulted.
    other["column_values"][-1].update(display_value="Other requester", mirrored_items=[])
    case.gateway.project["subitems"].insert(0, other)
    case.gateway.project["column_values"].append({
        "id": "lookup_mm0mm6ff", "type": "mirror", "column": {"title": "Contact"},
        "display_value": "Other requester, Example, Sam",
        "mirrored_items": [{
            "linked_item": {"id": entry["id"], "board": entry["board"]},
            "mirrored_value": {"__typename": "MirrorValue"},
        } for entry in case.gateway.project["subitems"]],
    })
    case.drawing_contact = contact
    return case


def test_selected_drawing_contact_publishes_despite_project_contact_rollup(contact_case):
    case = contact_case
    assert run(case) == ["published"]
    source = current(case).source_json
    assert source["drawingItemId"] == "9101"
    assert source["raw"]["Contact"] == "Example, Sam"
    assert source["sources"]["Contact"]["itemId"] == "9101"
    assert source["sources"]["Contact"]["columnId"] == "mirror_11__1"
    assert ["888", "777"] in source["dependencies"]
    assert ["666", "555"] in source["dependencies"]
    assert [read["columnId"] for read in source["crmReads"] if read["field"] == "Contact"] == ["mirror_11__1"]
    artifact = case.db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, current(case).current_revision))
    assert artifact.translation_json["values"]["Contact"] == "Example, Sam"


@pytest.mark.parametrize("contact_state", ["empty", "missing", "no_drawings"])
def test_contact_fallback_uses_email_without_project_rollup(contact_case, contact_state):
    case = contact_case
    if contact_state == "empty":
        case.drawing_contact.update(display_value="", mirrored_items=[])
    elif contact_state == "missing":
        case.gateway.project["subitems"][1]["column_values"].remove(case.drawing_contact)
    else:
        case.gateway.project["subitems"] = []
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["raw"]["Contact"] == "Alex Example"
    assert source["sources"]["Contact"]["source"] == "Email/Attachment"
    assert not any(read["columnId"] == "lookup_mm0mm6ff" for read in source["crmReads"])


@pytest.mark.parametrize("failure", ["unresolved", "conflicting"])
def test_invalid_selected_contact_retains_previous_pdf(contact_case, failure):
    case = contact_case
    assert run(case) == ["published"]
    revision = current(case).current_revision
    if failure == "unresolved":
        case.drawing_contact["mirrored_items"] = []
    else:
        value = case.drawing_contact["mirrored_items"][0]["mirrored_value"]
        value["linked_item_ids"].append("556")
        value["linked_items"].append({"id": "556", "name": "Another requester", "board": {"id": "666"}})
    assert run(case) == ["failed"]
    assert current(case).current_revision == revision
    assert len(case.gateway.uploads) == 1 and not case.gateway.deletes
    if failure == "unresolved":
        assert current(case).review_json["status"] == "source_failure"
    else:
        assert current(case).review_json["status"] == "conflict"
        assert "Contact" in current(case).review_json["issues"]


def test_contact_selection_preserves_holding_override(contact_case):
    case = contact_case
    override = column("request_contact", "Contact", "Holding requester")
    case.gateway.holding["column_values"].append(override)
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["raw"]["Contact"] == "Holding requester"
    assert source["sources"]["Contact"]["itemId"] == ITEM
    override["text"] = None
    assert resolve_specification_sources(case.gateway, case.item)["raw"]["Contact"] == "Example, Sam"


def test_ambiguous_drawing_does_not_choose_contact_from_project_rollup(contact_case):
    case = contact_case
    case.gateway.project["subitems"][1]["name"] = "TP00901_26.03 - A"
    assert run(case) == ["failed"]
    assert "missing or ambiguous" in current(case).review_json["issues"]["Drawing Reference"]
    assert not case.gateway.uploads


def test_project_contact_requires_explicit_mapping(case, monkeypatch):
    case.gateway.project["column_values"].append(column("request_contact", "Contact", "Project requester"))
    assert resolve_specification_sources(case.gateway, case.item)["raw"]["Contact"] == "Alex Example"
    monkeypatch.setattr(settings, "performance_specification_column_map", {"project": {"Contact": "request_contact"}})
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["raw"]["Contact"] == "Project requester"
    assert source["sources"]["Contact"]["columnId"] == "request_contact"
    case.gateway.project["column_values"].pop()
    with pytest.raises(SpecificationSourceError, match="Configured project column for Contact is missing"):
        resolve_specification_sources(case.gateway, case.item)


def test_explicit_project_contact_mapping_still_validates_source(contact_case, monkeypatch):
    case = contact_case
    monkeypatch.setattr(settings, "performance_specification_column_map", {"project": {"Contact": "lookup_mm0mm6ff"}})
    with pytest.raises(SpecificationSourceError, match="Unsupported or unreadable mirrored"):
        resolve_specification_sources(case.gateway, case.item)


def test_resolves_current_crm_and_keeps_optional_roof_number_and_raw_insulation(case):
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["raw"]["Drawing Title"] == "Warehouse South"
    assert source["raw"]["Company"] == "Acme, Limited"
    assert source["raw"]["Target U Value"] == "0.2"
    assert source["raw"]["Bauder Roof Num"] == "007"
    assert source["raw"]["Tapered Insulation"] == "TR27"
    assert source["raw"]["Incoming Information"] == ""
    assert ["9000", "9101"] in source["dependencies"]


@pytest.mark.parametrize("value,expected", [("A", "26.01 - A"), ("26.01 - A", "26.01 - A")])
def test_revision_mirror_cannot_shorten_tp_reference_suffix(case, value, expected):
    case.gateway.project["subitems"][0]["column_values"].append(column("mirror_1__1", "Revision", value))
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["raw"]["Revision"] == expected
    assert source["sources"]["Revision"]["observedRevision"] == value


def test_conflicting_crm_revision_publishes_both_values_for_review(case):
    case.gateway.project["subitems"][0]["column_values"].append(column("mirror_1__1", "Revision", "B"))
    assert run(case) == ["published"]
    assert current(case).review_json["status"] == "published_with_review"
    assert "Revision conflicts" in current(case).review_json["issues"]["Revision"]
    text = pdf_text(case.gateway.uploads[-1][1])
    assert "Revision: Review required" in text
    assert "Selected drawing revision: 26.01 - A" in text and "Reported revision: B" in text


def test_known_unmapped_product_publishes_review_details_and_reuses_cached_pdf(case):
    assert run(case) == ["published"]
    previous_revision = current(case).current_revision
    case.gateway.project["subitems"][0]["column_values"][1]["text"] = "BauderROCK"
    calls = []

    def generate(model, prompt, config):
        calls.append(prompt)
        raw = json.loads(prompt.split("\nResolved values: ", 1)[1])
        raw["Tapered Insulation"] = None
        payload = translation_payload(raw)
        payload["warnings"] = ["BauderROCK does not establish an exact CAD product variant."]
        assert "fieldReviews" not in config.response_json_schema["properties"]
        return SimpleNamespace(parsed=payload)

    case.client = LegacyGeminiClient("gemini-3.8-flash", generate_content=generate)
    assert run(case) == ["published"]
    assert len(calls) == 2  # One bounded evidence-aware recovery pass.
    assert "Shared field meanings" in calls[0]
    assert "sourceManifest" in calls[0]
    assert current(case).current_revision != previous_revision
    assert current(case).review_json["status"] == "published_with_review"
    assert current(case).review_json["source"]["raw"]["Tapered Insulation"] == "BauderROCK"
    assert len(case.gateway.uploads) == 2 and case.gateway.deletes
    text = pdf_text(case.gateway.uploads[-1][1])
    assert "Tapered Insulation: Review required" in text
    assert "BauderROCK does not establish an exact CAD product variant." in text
    assert "Tapered Insulation: Not Provided" not in text
    assert "Target Min U Value: Not Provided" in text
    assert current(case).last_error is None and current(case).checked_at is not None
    assert run(case) == ["unchanged"]
    assert len(calls) == 2 and current(case).review_json["status"] == "published_with_review"


def test_recovery_recovers_populated_text_instead_of_missing_marker(case):
    raw = resolve_specification_sources(case.gateway, case.item)["raw"]
    first, second = translation_payload(raw), translation_payload(raw)
    first["values"]["Contact"] = None
    responses = iter([first, second])
    client = LegacyGeminiClient("gemini-3.8-flash", generate_content=lambda *args: SimpleNamespace(parsed=next(responses)))
    assert pdf_values(translate_specification(raw, client))["Contact"] == "Alex Example"


def test_extracted_candidate_is_used_even_if_flat_scalar_was_missing(case):
    case.item.extracted_parameters_json["performanceParameters"]["Contact"] = None
    assert resolve_specification_sources(case.gateway, case.item)["raw"]["Contact"] == "Alex Example"


def test_forged_excerpt_and_incomplete_source_manifest_preserve_last_pdf(case):
    assert run(case) == ["published"]
    revision = current(case).current_revision
    extraction = deepcopy(case.item.extracted_parameters_json)
    extraction["sourceManifest"][0]["status"] = "failed:TimeoutError"
    case.item.extracted_parameters_json = extraction
    case.db.commit()
    assert run(case) == ["failed"]
    assert current(case).current_revision == revision and len(case.gateway.uploads) == 1
    extraction["sourceManifest"][0]["status"] = "read"
    next(e for e in extraction["specificationEvidence"] if e["field"] == "Contact")["snippet"] = "An invented excerpt"
    case.item.extracted_parameters_json = deepcopy(extraction)
    case.db.commit()
    assert run(case) == ["failed"]
    assert "Contact" in current(case).review_json["issues"]


def roof_evidence(case, values):
    extraction = deepcopy(case.item.extracted_parameters_json)
    extraction["performanceParameters"]["Decking"] = None
    extraction["specificationEvidence"] = [e for e in extraction["specificationEvidence"] if e["field"] != "Decking"]
    for scope, value in values:
        snippet = f"{scope}: Decking {value}"
        extraction["sourceManifest"][0]["text"] += "\n" + snippet
        extraction["specificationEvidence"].append({"field": "Decking", "value": value, "scope": scope,
            "source_id": "email:body", "location": "roof schedule", "snippet": snippet,
            "issuer": None, "role": "requested", "rationale": "Roof-specific requested deck"})
    case.item.extracted_parameters_json = extraction


def test_different_roof_values_are_not_flattened_and_confirmed_scope_resolves(case):
    roof_evidence(case, [("Roof 1", "Timber (18mm)"), ("Roof 5", "Concrete")])
    assert run(case) == ["published"]
    assert current(case).review_json["status"] == "published_with_review"
    artifact = case.db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, current(case).current_revision))
    assert artifact.translation_json["values"]["Decking"] is None
    text = pdf_text(case.gateway.uploads[-1][1])
    assert "Decking: Review required" in text
    assert "Timber (18mm) (Roof 1)" in text and "Concrete (Roof 5)" in text
    assert "Decking: Not Provided" not in text
    case.gateway.holding["column_values"].append(column("roof", "Bauder Roof Num", "5"))
    assert run(case) == ["published"]
    assert current(case).source_json["raw"]["Decking"] == "Concrete"
    assert current(case).review_json is None
    assert "Decking: Concrete" in pdf_text(case.gateway.uploads[-1][1])


def test_roof_conflict_and_unmapped_material_publish_together(case):
    roof_evidence(case, [("Roof 1", "Timber (18mm)"), ("Roof 5", "Concrete")])
    case.gateway.project["subitems"][0]["column_values"][1]["text"] = "BauderROCK"
    calls = []
    def generate(model, prompt, config):
        calls.append(prompt)
        raw = json.loads(prompt.split("\nResolved values: ", 1)[1])
        assert raw["Decking"] is None
        raw["Tapered Insulation"] = None
        return SimpleNamespace(parsed=translation_payload(raw))
    case.client = LegacyGeminiClient("offline", generate_content=generate)
    assert run(case) == ["published"] and len(calls) == 2
    assert set(current(case).review_json["issues"]) == {"Decking", "Tapered Insulation"}
    text = pdf_text(case.gateway.uploads[-1][1])
    assert "Timber (18mm) (Roof 1)" in text and "Concrete (Roof 5)" in text
    assert "Translation unresolved for source value 'BauderROCK'" in text
    assert "No verified CAD translation could be selected." in text


def test_unverified_conflicting_candidate_still_prevents_publication(case):
    roof_evidence(case, [("Roof 1", "Timber (18mm)"), ("Roof 5", "Concrete")])
    extraction = deepcopy(case.item.extracted_parameters_json)
    bad = {**extraction["specificationEvidence"][-1], "scope": "Roof 9", "snippet": "Invented excerpt"}
    extraction["specificationEvidence"].append(bad)
    case.item.extracted_parameters_json = extraction
    assert run(case) == ["failed"]
    assert not case.gateway.uploads and current(case).review_json["status"] == "conflict"


def test_model_cannot_choose_one_conflicting_deck_or_inject_review_notes(case):
    roof_evidence(case, [("Roof 1", "Timber (18mm)"), ("Roof 5", "Concrete")])
    def generate(model, prompt, config):
        raw = json.loads(prompt.split("\nResolved values: ", 1)[1])
        raw["Decking"] = "Concrete"
        return SimpleNamespace(parsed=translation_payload(raw))
    case.client = LegacyGeminiClient("offline", generate_content=generate)
    assert run(case) == ["failed"] and not case.gateway.uploads
    with pytest.raises(ValidationError):
        CADTranslation.model_validate({**translation_payload({}), "fieldReviews": {"Decking": "Invented review"}})


def test_shadow_review_and_lost_upload_reuse_review_notes(case, monkeypatch):
    roof_evidence(case, [("Roof 1", "Timber (18mm)"), ("Roof 5", "Concrete")])
    monkeypatch.setattr(settings, "design_processing_mode", "shadow")
    assert run(case) == ["rendered"] and not case.gateway.uploads
    assert current(case).review_json["status"] == "rendered_with_review"
    monkeypatch.setattr(settings, "design_processing_mode", "enabled")
    def timeout():
        case.gateway.on_upload = None
        raise TimeoutError("lost upload response")
    case.gateway.on_upload = timeout
    assert run(case) == ["failed"]
    assert run(case) == ["unchanged"]
    assert current(case).review_json["status"] == "published_with_review"
    assert len(case.gateway.uploads) == len(case.calls) == 1


def test_source_changes_during_review_translation_remain_fenced(case):
    roof_evidence(case, [("Roof 1", "Timber (18mm)"), ("Roof 5", "Concrete")])
    case.on_translate.append(lambda: case.gateway.holding["column_values"].append(column("roof", "Bauder Roof Num", "5")))
    assert run(case) == ["superseded"] and not case.gateway.uploads


def test_long_review_details_are_selectable_and_keep_field_order(case):
    values = translation_payload({})
    details = "Conflict: " + "; ".join(f"Roof {i} <north> & south: Timber (18mm)" for i in range(100))
    publication = SpecificationPublication.model_validate({**values, "fieldReviews": {"Decking": details}})
    text = pdf_text(render_performance_specification_pdf(publication, item_id=ITEM, revision="review"))
    assert "Roof 0 <north> & south" in text and "Roof 99 <north> & south" in text
    assert [text.index(label + ":") for label in FIELD_NAMES] == sorted(text.index(label + ":") for label in FIELD_NAMES)
    assert "Decking: Not Provided" not in text


def test_common_value_for_multiple_roofs_is_usable(case):
    roof_evidence(case, [("Roof 1", "Concrete"), ("Roof 5", "Concrete")])
    assert resolve_specification_sources(case.gateway, case.item)["raw"]["Decking"] == "Concrete"


@pytest.mark.parametrize("verified", [True, False])
def test_reviewed_crm_contact_keeps_email_alternative_and_source_labels(case, verified):
    extraction = deepcopy(case.item.extracted_parameters_json)
    extraction["sourceManifest"].append({
        "sourceId": "reviewed-crm:9000:9101", "sourceType": "reviewed_crm",
        "filename": "Monday CRM drawing TP00901_26.01 - A (reviewed snapshot)",
        "contentSha256": "crm-snapshot-hash", "status": "read", "text": "Contact: Sam Example",
        "observedAt": None, "reviewedAt": "2026-10-31T12:00:00+00:00",
    })
    extraction["specificationEvidence"].append({
        "field": "Contact", "value": "Sam Example", "source_id": "reviewed-crm:9000:9101",
        "snippet": "Contact: Sam Example" if verified else "Invented: Sam Example",
        "location": "Drawing Contact mirror", "scope": None, "issuer": None,
        "role": "requested", "request_role": "direct_request", "message_date": None,
        "rationale": "Reviewed CRM requester alternative; preserve the email disagreement for review",
    })
    case.item.extracted_parameters_json = extraction
    case.db.commit()
    if not verified:
        assert run(case) == ["failed"]
        assert current(case).review_json["status"] == "conflict"
        assert not case.gateway.uploads
        return
    assert run(case) == ["published"]
    record = current(case)
    assert record.review_json["status"] == "published_with_review"
    provenance = record.source_json["sources"]["Contact"]
    assert {c["value"] for c in provenance["candidates"]} == {"Alex Example", "Sam Example"}
    snapshot = next(s for s in record.source_json["sourceManifest"] if s["sourceType"] == "reviewed_crm")
    assert snapshot["observedAt"] is None and snapshot["reviewedAt"] == "2026-10-31T12:00:00+00:00"
    artifact = case.db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, record.current_revision))
    assert artifact.translation_json["values"]["Contact"] is None
    detail = artifact.translation_json["fieldReviews"]["Contact"]
    assert "Alex Example (request.eml)" in detail
    assert "Sam Example (Monday CRM drawing TP00901_26.01 - A (reviewed snapshot))" in detail
    text = pdf_text(case.gateway.uploads[-1][1])
    assert "Contact: Review required" in text and "Alex Example" in text and "Sam Example" in text


def test_people_mirror_preserves_name_with_comma_and_entity_identity(case):
    case.gateway.resolve_person = lambda kind, identity: "Example, Alex"
    case.gateway.project["subitems"][0]["column_values"].append({
        "id": "mirror92__1", "type": "mirror", "column": {"title": "Surveyor"},
        "mirrored_items": [{"linked_item": {"id": "777", "board": {"id": "888"}},
                            "mirrored_value": {"__typename": "PeopleValue", "text": "Example, Alex",
                                               "persons_and_teams": [{"id": "55", "kind": "person"}]}}]})
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["raw"]["Surveyor"] == "Example, Alex"
    assert ["888", "777"] in source["dependencies"]
    saved = source["sources"]["Surveyor"]["columnValue"]["mirrored_items"][0]["mirrored_value"]["resolvedPeople"]
    assert saved == [{"id": "55", "kind": "person", "name": "Example, Alex"}]


def test_unreadable_people_is_not_a_missing_surveyor(case):
    case.gateway.project["subitems"][0]["column_values"].append({
        "id": "mirror92__1", "type": "people", "text": "Alex", "column": {"title": "Surveyor"}})
    with pytest.raises(SpecificationSourceError, match="People"):
        resolve_specification_sources(case.gateway, case.item)


def test_artifact_keeps_immutable_input_snapshot_after_subsequent_refresh(case):
    assert run(case) == ["published"]
    original = case.db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, current(case).current_revision))
    saved = deepcopy(original.source_json)
    case.gateway.project["column_values"][0]["text"] = "Changed title"
    assert run(case) == ["published"]
    case.db.refresh(original)
    assert original.source_json == saved
    assert original.source_json["raw"]["Drawing Title"] == "Warehouse South"
    assert original.source_json["sources"]["Contact"]["selected"]["snippet"] == "Contact: Alex Example"


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


def test_cad_schema_rejection_recovers_without_weakening_value_validation(case):
    class SchemaRejected(Exception):
        code = 400
    raw = resolve_specification_sources(case.gateway, case.item)["raw"]
    calls = []
    def generate(model, prompt, config):
        calls.append(config)
        if config.response_json_schema:
            raise SchemaRejected()
        payload = translation_payload(raw)
        payload["values"]["Bauder Roof Num"] = "7"  # Original is 007.
        return SimpleNamespace(parsed=None, text=json.dumps(payload))
    client = LegacyGeminiClient("offline", generate_content=generate)
    with pytest.raises(ValueError, match="changed source text"):
        translate_specification(raw, client)
    assert len(calls) == 2 and calls[1].response_json_schema is None


def test_model_cannot_replace_an_unsupported_scale_with_a_nearest_allowed_scale(case):
    raw = resolve_specification_sources(case.gateway, case.item)["raw"]
    raw["Scale"] = "1:200"
    payload = translation_payload(raw)
    payload["values"]["Scale"] = "1:250"
    client = LegacyGeminiClient("gemini-3.8-flash", generate_content=lambda *args: SimpleNamespace(parsed=payload))
    with pytest.raises(ValueError, match="specified scale"):
        translate_specification(raw, client)


@pytest.mark.parametrize("label,source,changed", [("Page Size", "ISO A1", "ISO A3"),
                                                ("Decking", "Concrete", "Metal"),
                                                ("Scale", "Not to scale", "1:100")])
def test_translation_cannot_change_a_canonical_option_or_invent_an_unspecified_ratio(case, label, source, changed):
    raw = resolve_specification_sources(case.gateway, case.item)["raw"]
    raw[label] = source
    payload = translation_payload(raw)
    payload["values"][label] = changed
    client = LegacyGeminiClient("offline", generate_content=lambda *args: SimpleNamespace(parsed=payload))
    with pytest.raises(ValueError, match="changed"):
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


def test_confirmed_clear_uses_available_email_value_on_subsequent_refreshes(case):
    assert run(case) == ["published"]
    case.gateway.project["subitems"][0]["column_values"][0]["text"] = None
    assert run(case) == ["published"]
    record = current(case)
    assert record.source_json["raw"]["Target U Value"] == "0.18"
    assert record.source_json["sources"]["Target U Value"]["source"] == "Email/Attachment"
    assert any(read["field"] == "Target U Value" and read["status"] == "empty" for read in record.source_json["crmReads"])
    artifact = case.db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, record.current_revision))
    assert artifact.translation_json["values"]["Target U Value"] == "0.18"
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


def test_request_without_drawing_reference_does_not_merge_linked_drawings(case):
    extra = deepcopy(case.gateway.project["subitems"][0])
    extra.update(id="9102", name="TP00901_26.02 - A")
    case.gateway.project["subitems"].append(extra)
    case.item.extracted_parameters_json["performanceParameters"]["Drawing Reference"] = None
    case.item.extracted_parameters_json = with_evidence(case.item.extracted_parameters_json)
    source = resolve_specification_sources(case.gateway, case.item)
    assert source["drawingItemId"] is None
    assert source["drawingSelection"]["status"] == "unassigned"
    assert "Drawing Reference" in source["issues"]
    assert source["raw"]["Target U Value"] == "0.18"


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


def test_allowlist_holds_older_unlisted_specification_without_model_or_crm_calls(case, monkeypatch):
    # Queue work while enabled, then isolate a different test item.
    worker.enqueue_specification(case.db, BOARD, ITEM, immediate=True)
    case.db.commit()
    pending = current(case)
    due, generation = pending.scheduled_for, pending.requested_generation
    monkeypatch.setattr(settings, "design_processing_mode", "allowlist")
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", ["3320074395"])
    case.gateway.fetch_target = lambda *args: pytest.fail("Unlisted item read Monday")
    assert worker.claim_specification(case.db) is None
    assert worker.run_specifications_once(case.db, gateway=case.gateway, client=case.client, storage=case.storage) == []
    case.db.refresh(pending)
    assert pending.scheduled_for == due and pending.requested_generation == generation
    assert pending.lease_token is None and not case.calls and not case.storage.objects


def test_allowlisted_item_can_translate_and_publish(case, monkeypatch):
    monkeypatch.setattr(settings, "design_processing_mode", "allowlist")
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", [ITEM])
    assert run(case) == ["published"]
    assert len(case.calls) == len(case.gateway.uploads) == 1


def test_specification_claim_filters_before_older_unlisted_work(case, monkeypatch):
    case.db.add(DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id="100", state="scheduled", warnings_json=[]))
    case.db.commit()
    other = worker.enqueue_specification(case.db, BOARD, "100", immediate=True)
    other.scheduled_for = worker.utc_now() - timedelta(hours=1)
    worker.enqueue_specification(case.db, BOARD, ITEM, immediate=True)
    case.db.commit()
    monkeypatch.setattr(settings, "design_processing_mode", "allowlist")
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", [ITEM])
    assert worker.claim_specification(case.db)[1] == ITEM
    case.db.refresh(other)
    assert other.lease_token is None and other.completed_generation == 0


def test_specification_discovery_filters_before_limit(case, monkeypatch):
    case.db.add(DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id="100", state="scheduled", warnings_json=[]))
    for item_id in ("100", ITEM):
        case.db.add(Task(external_task_key=f"acct:{BOARD}:{item_id}", account_id="acct", board_id=BOARD,
            item_id=item_id, auto_sync_state="active", auto_sync_enabled=True, source_group_id="topics", sync_status="completed"))
    case.db.commit()
    monkeypatch.setattr(settings, "design_processing_mode", "allowlist")
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", [ITEM])
    assert worker.discover_specifications(case.db, limit=1) == 1
    assert current(case) is not None
    assert case.db.get(PerformanceSpecification, (BOARD, "100")) is None


def test_allowlist_change_after_claim_fences_translation(case, monkeypatch):
    worker.enqueue_specification(case.db, BOARD, ITEM, immediate=True)
    case.db.commit()
    claim = worker.claim_specification(case.db)
    monkeypatch.setattr(settings, "design_processing_mode", "allowlist")
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", ["3320074395"])
    case.gateway.fetch_target = lambda *args: pytest.fail("Unlisted claimed item read Monday")
    assert worker.execute_specification(case.db, claim, gateway=case.gateway, client=case.client, storage=case.storage) == "superseded"
    assert not case.calls and not case.gateway.uploads and current(case).scheduled_for is not None


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


def test_reconciliation_preserves_specification_retry_deadline_and_attempts(case):
    record = worker.enqueue_specification(case.db, BOARD, ITEM, group_id="topics", immediate=True)
    record.attempt_count = 5
    record.last_error = "Waiting for current extraction"
    record.scheduled_for = worker.utc_now() + timedelta(minutes=30)
    case.db.commit()
    due, generation = record.scheduled_for, record.requested_generation
    worker.enqueue_specification(case.db, BOARD, ITEM, group_id="topics", reconcile=True)
    case.db.commit()
    assert worker._aware(current(case).scheduled_for) == worker._aware(due)
    assert current(case).attempt_count == 5 and current(case).requested_generation == generation
    worker.enqueue_specification(case.db, BOARD, ITEM, group_id="topics")
    case.db.commit()
    assert worker._aware(current(case).scheduled_for) == worker._aware(due) and current(case).attempt_count == 5
    assert current(case).requested_generation == generation + 1


def test_missing_roof_number_is_optional_and_incoming_information_is_always_blank(case):
    case.item.extracted_parameters_json["performanceParameters"]["Bauder Roof Num"] = None
    case.item.extracted_parameters_json = with_evidence(case.item.extracted_parameters_json)
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
    assert "output-v9" in current(case).last_error
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
