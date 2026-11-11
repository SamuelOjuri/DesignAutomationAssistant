"""Phase 3 recommendation preparation is read-only and independent of intake fields."""

from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from backend.app.services.design_processing_pipeline import _extraction_payload
from backend.app.services.design_processing_state import ProcessingIdentity
from backend.app.services.design_processing_target import DesignProcessingTargetMismatch
from backend.app.services.legacy_enquiry.analysis import LegacyAnalysisResult
from backend.app.services.match_report import MatchReport, render_match_report_pdf
from backend.app.services.performance_specification_sources import prepare_specification_candidates
from backend.app.services.specification_intent import build_request_intent
from backend.app.services.specification_references import REFERENCE_PARSER_VERSION
from backend.app.services.specification_review_contract import CandidateLookup
from backend.app.services.specification_semantics import EVIDENCE_VERSION, PDF_FIELD_NAMES
from backend.tests.test_design_processing_phase5 import db_session


BOARD = "700"
MANIFEST = [{"sourceId": "mail:body", "sourceType": "email_body", "status": "read",
    "text": "From: Alex <alex@client.example>\nTo: design@taperedplus.co.uk\n"
        "Date: Mon, 9 Nov 2026 10:00:00 +0000\nSubject: AB00123_26.01 - A\n\n"
        "Please adjust the existing scheme AB00123_26.01 - A to suit the new outlet."}]


def match_contract(count=1):
    return {"candidates": [{"rank": index, "mondayItemId": str(index),
        "projectReference": "AB00123", "projectTitle": "Existing roof", "similarity": 0.3,
        "matchPercentage": "30.0%", "matchKind": "exact_reference",
        "matchRationale": "Printed project reference matches the selected request"}
        for index in range(1, count + 1)]}


def drawing(identity, reference, state="active"):
    return {"id": str(identity), "name": reference, "state": state, "board": {"id": "701"}}


class ReadOnlyGateway:
    def __init__(self, drawings=None):
        self.reads = []
        self.project = {"id": "1", "name": "AB00123", "state": "active", "board": {"id": BOARD},
            "column_values": [{"id": "account", "type": "board_relation", "column": {"title": "Company"},
                "linked_item_ids": ["81"], "linked_items": [{"id": "81", "name": "Client Limited", "board": {"id": "800"}}]}],
            "subitems": drawings or [drawing(10, "AB00123_26.01 - A"), drawing(11, "AB00123_26.01 - AA"),
                drawing(12, "AB00123_26.02 - B")]}

    def fetch_specification_project(self, identity):
        self.reads.append(identity)
        return {**deepcopy(self.project), "id": identity}


def test_payload_persists_current_versions_and_typed_intent_without_changing_labels():
    intent = build_request_intent(MANIFEST)
    result = LegacyAnalysisResult(parameters={}, sources={}, project_name="Existing roof",
        email_content_audit=(), extracted_text_sha256="abc", source_manifest=tuple(MANIFEST),
        specification_request_intent=intent.model_dump(mode="json"))
    payload = _extraction_payload(result, ProcessingIdentity("email-1", "pipeline-1"))
    assert payload["specificationEvidenceVersion"] == EVIDENCE_VERSION
    assert payload["requestIntentSchemaVersion"] == 1
    assert payload["referenceParserVersion"] == REFERENCE_PARSER_VERSION
    assert payload["specificationRequestIntent"]["recommended_type"] == "amendment"
    assert len(PDF_FIELD_NAMES) == 18


def test_detected_target_prepares_candidates_without_any_holding_item_or_business_writes():
    gateway = ReadOnlyGateway()
    intent = build_request_intent(MANIFEST).model_dump(mode="json")
    snapshot = prepare_specification_candidates(gateway, match_contract(), intent, project_board_id=BOARD)
    lookup = CandidateLookup.model_validate(snapshot["lookup"])
    assert lookup.status == "available"
    assert lookup.projects[0].project_reference == "AB00123"
    assert lookup.projects[0].project_title == "Existing roof"
    assert gateway.reads == ["1"]
    selection = snapshot["drawingSelections"][0]
    assert selection["selectedItemId"] == "10"  # Relevant existing target wins over newer AA.
    assert len(selection["recommendations"]) == 2  # Both roof series stay visible.
    assert snapshot["accountRecommendations"][0]["value"] == "Client Limited"
    assert ["800", "81"] in snapshot["dependencies"]
    assert all("Target U Value" not in candidate for candidate in lookup.drawings)


@pytest.mark.parametrize("raw_reference,resolved_reference,expected", [
    ("00123", "AB00123", "00123"),
    (None, "AB00123", "AB00123"),
])
def test_project_display_labels_use_matching_metadata_without_detail_reads(raw_reference, resolved_reference, expected):
    contract = match_contract()
    contract["candidates"][0].update(projectReference=raw_reference, resolvedProjectReference=resolved_reference)
    snapshot = prepare_specification_candidates(SimpleNamespace(), contract, {}, project_board_id=BOARD)
    project = snapshot["lookup"]["projects"][0]
    assert project["project_reference"] == expected
    assert project["project_title"] == "Existing roof"
    assert project["record"] == {"board_id": BOARD, "item_id": "1"}


def test_existing_project_detail_read_fills_missing_display_labels():
    gateway = ReadOnlyGateway()
    gateway.project["column_values"].append({"id": "text3__1", "text": "Current project title"})
    contract = match_contract()
    contract["candidates"][0].pop("projectReference")
    contract["candidates"][0].pop("projectTitle")
    snapshot = prepare_specification_candidates(gateway, contract, {}, project_board_id=BOARD)
    project = snapshot["lookup"]["projects"][0]
    assert project["project_reference"] == "AB00123"
    assert project["project_title"] == "Current project title"
    assert gateway.reads == ["1"]


def test_multiple_email_targets_cannot_silently_select_one_roof():
    intent = {"detected_references": [{"literal": reference, "kind": "drawing", "role": "current_request"}
        for reference in ("AB00123_26.01 - A", "AB00123_26.02 - B")]}
    snapshot = prepare_specification_candidates(ReadOnlyGateway(), match_contract(), intent, project_board_id=BOARD)
    assert snapshot["drawingSelections"][0]["selectedItemId"] is None
    assert snapshot["drawingSelections"][0]["status"] == "ambiguous"


def test_unavailable_and_valid_empty_inventories_are_distinct_and_archived_is_never_selected():
    gateway = ReadOnlyGateway([drawing(10, "AB00123_26.01 - A", "archived")])
    archived = prepare_specification_candidates(gateway, match_contract(), {}, project_board_id=BOARD)
    assert archived["drawingSelections"][0]["selectedItemId"] is None
    assert archived["lookup"]["status"] == "available"
    gateway.project["subitems"] = []
    empty = prepare_specification_candidates(gateway, match_contract(), {}, project_board_id=BOARD)
    assert empty["drawingSelections"][0]["status"] == "empty"
    gateway.project["subitems"] = None
    unavailable = prepare_specification_candidates(gateway, match_contract(), {}, project_board_id=BOARD)
    assert unavailable["lookup"]["status"] == "unavailable"


def test_broad_search_remains_visible_with_bounded_detail_reads():
    gateway = ReadOnlyGateway()
    snapshot = prepare_specification_candidates(gateway, match_contract(20), {}, project_board_id=BOARD)
    assert len(gateway.reads) == 12
    assert len(snapshot["lookup"]["projects"]) == 20
    assert snapshot["lookup"]["status"] == "pending"


def test_scope_cancellation_propagates_from_optional_candidate_read():
    class Cancelled:
        def fetch_specification_project(self, identity):
            raise DesignProcessingTargetMismatch("ineligible", "Item left scope")
    with pytest.raises(DesignProcessingTargetMismatch):
        prepare_specification_candidates(Cancelled(), match_contract(), {}, project_board_id=BOARD)


def test_report_identifies_recommendation_and_keeps_long_explanation_inside_page():
    intent = build_request_intent(MANIFEST).model_dump(mode="json")
    intent["explanation"] = "The selected request changes the existing scheme. " * 150
    contract = {**match_contract(), "specificationRequestIntent": intent}
    report = MatchReport.from_contract(source_item_id="100", extracted_company="Client", match_contract=contract)
    document = PdfReader(io.BytesIO(render_match_report_pdf(report)))
    text = "\n".join(page.extract_text() for page in document.pages)
    assert "Amendment (AI recommendation)" in text
    assert "Title similarity: 30.0%" in text
    assert "probability of an amendment" in text
    assert "Review the AI recommendation" in text
    assert len(document.pages) > 1
    for page in document.pages:
        positions = []
        page.extract_text(visitor_text=lambda text, cm, tm, font, size: positions.append(tm[5]) if text.strip() else None)
        assert all(30 <= position <= float(page.mediabox.height) - 30 for position in positions)


def test_analysis_worker_persists_enriched_reference_sidecar_and_candidates_without_business_writes(
    db_session, monkeypatch, tmp_path,
):
    from backend.app.models import DesignProcessingItem, DesignProcessingJob
    from backend.app.monday_client import MondayProjectBoardItem
    from backend.app.services import design_processing_pipeline
    from backend.app.services.design_processing_inputs import (
        DesignEmailAsset, DesignProcessingTargetSnapshot, compute_design_input_revision,
    )
    from backend.app.services.design_processing_worker import run_worker_once
    from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction, extract_parameters
    from backend.tests.test_design_processing_phase5 import (
        BOARD_ID, GROUP_ID, ITEM_ID, MemoryArtifactStorage, _queue,
    )

    content = MANIFEST[0]["text"].encode()
    source = tmp_path / "request.eml"
    source.write_bytes(content)
    asset = DesignEmailAsset("300", source.name, "eml", len(content), "2026-11-09T10:00:00Z",
        "https://monday.invalid/private/300", True)
    snapshot = DesignProcessingTargetSnapshot(BOARD_ID, ITEM_ID, GROUP_ID, "active", "New intake",
        (asset,), compute_design_input_revision((asset,)))
    intent = build_request_intent(MANIFEST).model_dump(mode="json")
    extracted = DesignParameterExtraction(**{label: None for label, field in
        DesignParameterExtraction.model_fields.items() if field.is_required()})
    parameters = extract_parameters("", extracted_parameters=extracted)
    parameters["Reason for Change"] = "Amendment (AI recommendation)"
    result = LegacyAnalysisResult(parameters=parameters, sources={label: "Email Content" for label in parameters},
        project_name="", email_content_audit=(), extracted_text_sha256=hashlib.sha256(content).hexdigest(),
        source_manifest=tuple(MANIFEST), specification_request_intent=intent)
    monkeypatch.setattr(design_processing_pipeline, "run_interruptible_analysis", lambda *args, **kwargs: result)

    class Gateway(ReadOnlyGateway):
        project_board_id = BOARD

        def __init__(self):
            super().__init__()
            self.reference_reads = []

        def fetch_target(self, identity):
            assert identity == ITEM_ID
            return snapshot

        def fetch_project_items_matching_references(self, references):
            self.reference_reads.append(references)
            return (MondayProjectBoardItem("1", "AB00123", "Existing roof", "active", "2017-01-01"),)

        def fetch_project_items_matching_full_text(self, *args, **kwargs):
            pytest.fail("A reference-only request must not trigger an empty-title directory search")

        def inspect_file_columns(self, identity):
            return {}

        def update_design_owned_columns(self, *args, **kwargs):
            pytest.fail("Recommendation preparation wrote a Monday business column")

        def upload_design_file(self, *args, **kwargs):
            pytest.fail("Shadow recommendation preparation uploaded a Monday file")

    gateway = Gateway()
    _queue(db_session, snapshot)
    worker = run_worker_once(db_session, worker_id="review-phase3", access_token="test-token",
        gateway=gateway, analysis_client=object(), artifact_storage=MemoryArtifactStorage(),
        downloader=lambda *args: SimpleNamespace(temp_path=str(source), content_type="message/rfc822",
            sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content)), mode="shadow",
        claim_limit=1, recover_leases=False, heartbeat_interval_seconds=0)
    assert worker.analyzed == 1 and worker.published == 0
    item = db_session.query(DesignProcessingItem).one()
    contract = item.match_result_json["result"]
    assert gateway.reference_reads == [("AB00123",)]
    assert gateway.reads == ["1"]
    assert contract["titleLookup"]["status"] == "not_requested"
    assert contract["candidates"][0]["matchKind"] == "exact_reference"
    assert contract["specificationCandidates"]["drawingSelections"][0]["selectedItemId"] == "10"
    saved_intent = item.extracted_parameters_json["specificationRequestIntent"]
    assert saved_intent["matched_projects"][0]["record"] == {"board_id": BOARD, "item_id": "1"}
    assert saved_intent == contract["specificationRequestIntent"]
    assert db_session.query(DesignProcessingJob).one().status == "completed"
