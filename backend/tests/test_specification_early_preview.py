"""Current extraction is readable before matching/rendering, without write gates."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from backend.app.config import settings
from backend.app.models import (
    DesignProcessingJob, PerformanceSpecification, PerformanceSpecificationArtifact,
    PerformanceSpecificationReview, PerformanceSpecificationApproval,
)
from backend.app.services import specification_review_workspace as workspace
from backend.app.services import design_processing_pipeline as pipeline
from backend.app.services.performance_specification_sources import (
    SpecificationSourceWaiting, resolve_specification_sources,
)
from backend.app.services.specification_review import ReviewConflict
from backend.app.services import specification_review as persistence
from backend.tests.test_performance_specification import case, with_evidence
from backend.tests.test_specification_review_persistence import ACTOR, ACCOUNT, source_snapshot


@pytest.fixture()
def pending(case):
    extraction = deepcopy(case.item.extracted_parameters_json)
    extraction["specificationRequestIntent"].update(recommended_type="new_enquiry", matched_projects=[])
    case.item.extracted_parameters_json = with_evidence(extraction)
    case.item.latest_analyzed_input_revision = "previous-input"
    case.item.match_result_json = None
    case.db.commit()
    return case


def read(case):
    return workspace.get_workspace(case.db, board_id=case.item.board_id,
        item_id=case.item.item_id, account_id=ACCOUNT, gateway=case.gateway, editable=True)


def test_validated_fields_appear_before_matching_or_legacy_render_without_creating_review(pending):
    payload = read(pending)
    assert payload["status"] == "analysis_pending" and payload["provisional"]
    assert payload["fields"]["Target U Value"]["effective"]["display_value"] == "0.18"
    assert payload["fields"]["Decking"]["effective"]["display_value"] == "Concrete"
    assert payload["candidates"]["status"] == "pending"
    assert payload["source_fingerprint"] is None and payload["pdf"] is None
    assert payload["analysis"]["current_extraction"] and not payload["analysis"]["ready"]
    assert not pending.db.query(PerformanceSpecificationReview).count()
    assert not pending.db.query(PerformanceSpecification).count()
    assert not pending.db.query(PerformanceSpecificationArtifact).count()


@pytest.mark.parametrize("change", ["input", "desired_pipeline", "payload_pipeline", "live_input"])
def test_stale_input_or_pipeline_never_supplies_provisional_fields(pending, change):
    extraction = deepcopy(pending.item.extracted_parameters_json)
    if change == "input":
        extraction["inputRevision"] = "old-input"
    elif change == "desired_pipeline":
        pending.item.latest_desired_pipeline_version = "old-pipeline"
    elif change == "payload_pipeline":
        extraction["pipelineVersion"] = "old-pipeline"
    else:
        pending.gateway.target.input_revision = "new-live-input"
    pending.item.extracted_parameters_json = extraction
    pending.db.commit()
    payload = read(pending)
    assert payload["status"] == "source_waiting" and not payload["provisional"]
    assert payload["fields"] == {} and payload["source"] is None


def test_invalid_evidence_never_becomes_an_early_preview(pending):
    extraction = deepcopy(pending.item.extracted_parameters_json)
    extraction["specificationEvidence"][0]["snippet"] = "This text is absent from the source"
    pending.item.extracted_parameters_json = extraction
    pending.db.commit()
    payload = read(pending)
    assert not payload["provisional"] and payload["fields"] == {}


def test_early_read_does_not_admit_publisher_or_saved_decisions(pending):
    read(pending)
    with pytest.raises(SpecificationSourceWaiting):
        resolve_specification_sources(pending.gateway, pending.item)
    with pytest.raises(ReviewConflict) as caught:
        workspace.save_decisions(pending.db, board_id=pending.item.board_id,
            item_id=pending.item.item_id, actor=ACTOR, gateway=pending.gateway,
            command={"expected_review_version": 0, "expected_source_fingerprint": "preview",
                     "idempotency_key": "early-save", "field_operations": {
                         "Decking": {"operation": "specify_blank"}}})
    assert caught.value.code == "source_waiting"
    assert not pending.db.query(PerformanceSpecificationReview).count()
    assert not pending.db.query(PerformanceSpecificationArtifact).count()


def test_provisional_read_keeps_existing_review_decisions_and_queue_untouched(pending):
    pending.db.add(PerformanceSpecification(board_id=pending.item.board_id, item_id=pending.item.item_id,
        requested_generation=0, completed_generation=0))
    pending.db.commit()
    persistence.initialize_review(pending.db, board_id=pending.item.board_id,
        item_id=pending.item.item_id, account_id=ACCOUNT, source=source_snapshot())
    before = pending.db.query(PerformanceSpecification).one().requested_generation
    pending.db.rollback()
    payload = read(pending)
    assert payload["provisional"] and payload["source_fingerprint"] is None
    assert payload["fields"]["Decking"]["decision"] is None
    assert payload["fields"]["Decking"]["effective"]["display_value"] == "Concrete"
    retained = pending.db.query(PerformanceSpecificationReview).one()
    assert retained.source_fingerprint == "sources-1" and retained.review_version == 0
    assert pending.db.query(PerformanceSpecification).one().requested_generation == before
    pending.db.rollback()
    polled = workspace.get_status(pending.db, board_id=pending.item.board_id,
        item_id=pending.item.item_id, account_id=ACCOUNT, editable=True)
    assert polled["provisional"] and polled["source_fingerprint"] is None


def test_provisional_poll_keeps_old_published_pdf_separate_and_hides_old_approval(pending):
    spec = PerformanceSpecification(board_id=pending.item.board_id, item_id=pending.item.item_id,
        requested_generation=0, completed_generation=0)
    pending.db.add(spec)
    pending.db.commit()
    persistence.initialize_review(pending.db, board_id=pending.item.board_id,
        item_id=pending.item.item_id, account_id=ACCOUNT, source=source_snapshot())
    review = pending.db.query(PerformanceSpecificationReview).one()
    artifact = PerformanceSpecificationArtifact(board_id=pending.item.board_id, item_id=pending.item.item_id,
        revision="old-published", source_json={}, translation_json={}, model_version="test",
        filename="previous.pdf", storage_bucket="private", storage_object_key="previous.pdf",
        content_sha256="a" * 64, size_bytes=10, monday_asset_id="91", status="published")
    pending.db.add(artifact)
    pending.db.flush()
    approval = PerformanceSpecificationApproval(monday_account_id=ACCOUNT,
        board_id=pending.item.board_id, item_id=pending.item.item_id, artifact_revision=artifact.revision,
        review_version=0, source_fingerprint=review.source_fingerprint, extraction_identity="old-extraction",
        decision_digest=review.decision_digest, actor_app_user_id=ACTOR.app_user_id,
        actor_monday_user_id=ACTOR.monday_user_id)
    pending.db.add(approval)
    pending.db.flush()
    review.current_approval_id = approval.id
    spec.current_revision = artifact.revision
    pending.db.commit()
    for payload in (read(pending), workspace.get_status(pending.db, board_id=pending.item.board_id,
            item_id=pending.item.item_id, account_id=ACCOUNT, editable=True)):
        assert payload["provisional"] and payload["status"] == "analysis_pending"
        assert payload["pdf"] is None and payload["approval"] is None
        assert payload["published_pdf"]["revision"] == "old-published"


def test_polling_reports_only_current_safe_progress_and_detects_full_analysis_completion(pending):
    now = datetime.now(timezone.utc)
    pending.db.add(DesignProcessingJob(board_id=pending.item.board_id, item_id=pending.item.item_id,
        trigger_type="source_changed", execution_kind="analysis", status="running", stage="matching",
        execution_input_revision=pending.item.latest_desired_input_revision,
        execution_pipeline_version=settings.design_processing_pipeline_version,
        scheduled_for=now, started_at=now, last_error="PRIVATE failure URL",
        result_json={"progress": {"phase": "reading_sources", "source_counts": {
            "total": 8, "read": 5, "skipped": 3, "failed": -1, "private_filename": "secret.pdf"},
            "source_text": "PRIVATE source text"}}))
    pending.db.commit()
    before = workspace.get_status(pending.db, board_id=pending.item.board_id,
        item_id=pending.item.item_id, account_id=ACCOUNT, editable=True)
    assert before["status"] == "analysis_pending" and before["provisional"]
    assert before["analysis"]["stage"] == "matching"
    assert before["analysis"]["source_counts"] == {"total": 8, "read": 5, "skipped": 3}
    assert before["analysis"]["started_at"]
    assert "PRIVATE" not in json.dumps(before) and "secret.pdf" not in json.dumps(before)
    pending.item.latest_analyzed_input_revision = pending.item.latest_desired_input_revision
    pending.db.commit()
    after = workspace.get_status(pending.db, board_id=pending.item.board_id,
        item_id=pending.item.item_id, account_id=ACCOUNT, editable=True)
    assert not after["provisional"] and after["analysis"]["ready"]
    assert after["extraction_identity"] == before["extraction_identity"]
    assert "fields" not in after and "source" not in after


def test_old_job_progress_is_not_reported_for_current_input(pending):
    pending.db.add(DesignProcessingJob(board_id=pending.item.board_id, item_id=pending.item.item_id,
        trigger_type="source_changed", execution_kind="analysis", status="running", stage="rendering",
        execution_input_revision="old-input", execution_pipeline_version=settings.design_processing_pipeline_version,
        scheduled_for=datetime.now(timezone.utc), result_json={"progress": {"source_counts": {"total": 999}}}))
    pending.db.commit()
    payload = workspace.get_status(pending.db, board_id=pending.item.board_id,
        item_id=pending.item.item_id, account_id=ACCOUNT, editable=True)
    assert payload["analysis"]["stage"] is None and payload["analysis"]["source_counts"] == {}


def test_pipeline_exposes_preview_at_render_hook_before_legacy_documents_exist(pending, monkeypatch):
    now = datetime.now(timezone.utc)
    pending.gateway.target.missing_name = pending.gateway.target.missing_email = False
    job = DesignProcessingJob(board_id=pending.item.board_id, item_id=pending.item.item_id,
        trigger_type="source_changed", execution_kind="analysis", status="running", stage="matching",
        execution_input_revision=pending.item.latest_desired_input_revision,
        execution_pipeline_version=settings.design_processing_pipeline_version,
        scheduled_for=now, locked_by="preview-worker", locked_at=now, started_at=now)
    pending.db.add(job)
    pending.db.commit()
    reached = []

    class StopBeforeLegacyRender(Exception):
        pass

    def inspect_before_render(**kwargs):
        payload = read(pending)
        assert payload["provisional"] and payload["status"] == "analysis_pending"
        assert payload["analysis"]["stage"] == "rendering"
        assert payload["fields"]["Decking"]["effective"]["display_value"] == "Concrete"
        assert pending.item.latest_analyzed_input_revision == "previous-input"
        assert not pending.db.query(PerformanceSpecificationArtifact).count()
        reached.append(payload)
        pending.db.rollback()
        raise StopBeforeLegacyRender()

    monkeypatch.setattr(pipeline, "render_analysis_artifacts", inspect_before_render)
    with pytest.raises(StopBeforeLegacyRender):
        pipeline.run_analysis_pipeline(pending.db, job.id, worker_id="preview-worker",
            access_token="unused", gateway=pending.gateway, analysis_client=None, artifact_storage=None,
            artifact_bucket="unused", pipeline_version=settings.design_processing_pipeline_version,
            expected_board_id=pending.item.board_id, expected_group_id=pending.gateway.target.group_id,
            mode="shadow")
    assert len(reached) == 1
