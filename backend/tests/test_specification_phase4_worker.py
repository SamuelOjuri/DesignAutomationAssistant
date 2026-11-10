"""Phase 4 publishes durable reviewer decisions without Monday business writes."""

from copy import deepcopy
import uuid

import pytest

from backend.app.models import (
    PerformanceSpecificationArtifact, PerformanceSpecificationApproval,
    PerformanceSpecificationReview, PerformanceSpecificationReviewEvent,
)
from backend.app.services import performance_specification as worker
from backend.app.services import specification_review as reviews
from backend.app.services.performance_specification_sources import resolve_specification_sources
from backend.app.services.specification_review_artifacts import build_review_artifact_metadata
from backend.app.services.specification_review_contract import (
    FIELD_NAMES, ReviewActor, ReviewDecisionState, ReviewSourceSnapshot, SaveReviewDecisions,
)
from backend.tests.test_performance_specification import (
    BOARD, ITEM, case, current, pdf_text, run,
)


ACTOR = ReviewActor(app_user_id="phase4-reviewer", monday_user_id="100", monday_account_id="200")


@pytest.fixture()
def review_case(case, monkeypatch):
    extracted = deepcopy(case.item.extracted_parameters_json)
    extracted["specificationRequestIntent"] = {
        "recommended_type": "new_enquiry", "explanation": "A new scheme is requested.",
        "evidence": [{"source_id": "email:body", "excerpt": "Drawing Title: Old title",
                      "location": "email body"}],
    }
    extracted["requestIntentSchemaVersion"] = 1
    case.item.extracted_parameters_json = extracted
    case.db.commit()
    # A new enquiry must work without a business-owned TP Ref relation.
    case.gateway.holding["column_values"][0].update(linked_item_ids=[], linked_items=[])

    def forbidden(*args, **kwargs):
        pytest.fail("Specification Review attempted a Monday business-field write")

    for name in ("update_column", "update_columns", "update_design_owned_columns",
                 "move_item_to_group", "create_item", "create_contact", "create_account"):
        monkeypatch.setattr(case.gateway.__class__, name, forbidden, raising=False)
    assert run(case) == ["published"]
    snapshot = ReviewSourceSnapshot.model_validate(current(case).source_json["reviewSource"])
    case.db.commit()
    reviews.initialize_review(case.db, board_id=BOARD, item_id=ITEM,
        account_id=ACTOR.monday_account_id, source=snapshot)
    case.snapshot = snapshot
    return case


def _save(case, operations, *, selections=None):
    record = case.db.get(PerformanceSpecificationReview, (BOARD, ITEM), populate_existing=True)
    state = reviews.review_state(record)
    resolved = resolve_specification_sources(case.gateway, case.item, review_state=state)
    source = ReviewSourceSnapshot.model_validate(resolved["reviewSource"])
    command = SaveReviewDecisions(
        expected_review_version=record.review_version,
        expected_source_fingerprint=record.source_fingerprint,
        idempotency_key=uuid.uuid4().hex,
        selections=selections, field_operations=operations,
    )
    case.db.commit()
    return reviews.save_review_decisions(case.db, board_id=BOARD, item_id=ITEM,
        actor=ACTOR, command=command, source=source)


def _publish(case):
    results = worker.run_specifications_once(case.db, gateway=case.gateway,
        client=case.client, storage=case.storage)
    assert results in (["published"], ["unchanged"])
    specification = current(case)
    artifact = case.db.get(PerformanceSpecificationArtifact,
        (BOARD, ITEM, specification.current_revision))
    return specification, artifact


def test_saved_values_blanks_and_revision_reach_exact_pdf_and_survive_refresh(review_case):
    case = review_case
    _save(case, {
        "Decking": {"operation": "specify_value", "display_value": "Custom timber – José's roof"},
        "Incoming Information": {"operation": "specify_value", "display_value": "Reviewer checked the roof survey"},
        "Revision": {"operation": "specify_value", "display_value": "Manual revision B"},
        "Company": {"operation": "specify_blank"},
    })
    specification, artifact = _publish(case)
    metadata = artifact.source_json["review"]
    state = reviews.review_state(case.db.get(PerformanceSpecificationReview, (BOARD, ITEM)))
    source = ReviewSourceSnapshot.model_validate(artifact.source_json["reviewSource"])
    assert metadata == build_review_artifact_metadata(state, source)
    assert metadata["review_version"] == 1
    assert metadata["field_provenance"]["Company"]["display_value"] == ""
    assert metadata["field_provenance"]["Company"]["source_label"] == "Reviewer Specified"
    content = case.storage.read_private(artifact.storage_bucket, artifact.storage_object_key)
    text = pdf_text(content)
    assert "Custom timber" in text and "José's roof" in text
    assert "Incoming Information: Reviewer checked the roof survey" in text
    assert "Revision: Manual revision B" in text
    assert text.count("Source: Reviewer Specified") == 4
    assert "Company: Not Provided" not in text
    original = specification.current_revision
    assert run(case) == ["unchanged"]
    assert current(case).current_revision == original
    assert reviews.review_state(case.db.get(PerformanceSpecificationReview, (BOARD, ITEM))) == state


def test_reviewer_acceptance_changes_publication_identity_without_another_model_request(review_case):
    case = review_case
    _, before = _publish(case)
    model_calls = len(case.calls)
    source = ReviewSourceSnapshot.model_validate(before.source_json["reviewSource"])
    _save(case, {"Decking": {"operation": "accept_recommendation",
                            "proposal_id": source.fields["Decking"].proposal.proposal_id}})
    _, after = _publish(case)
    assert after.revision != before.revision
    assert after.source_json["review"]["publication_identity"] != before.source_json["review"]["publication_identity"]
    assert len(case.calls) == model_calls
    field = after.source_json["review"]["field_provenance"]["Decking"]
    assert field["reviewer_accepted"] is True
    assert field["source_label"] == "Email Context"
    assert after.translation_json["values"] == before.translation_json["values"]


@pytest.mark.parametrize("stage", ["translation", "upload"])
def test_reviewer_save_during_remote_work_cannot_promote_stale_pdf(review_case, stage):
    case = review_case
    before_revision = current(case).current_revision
    _save(case, {"Decking": {"operation": "specify_value", "display_value": "First correction"}})

    def newer_decision():
        _save(case, {"Decking": {"operation": "specify_value", "display_value": "Latest correction"}})

    if stage == "translation":
        # Change a remaining automatic input so translation cannot come from cache.
        extracted = deepcopy(case.item.extracted_parameters_json)
        extracted["performanceParameters"]["Scale"] = "1 : 50"
        for fact in extracted["specificationEvidence"]:
            if fact["field"] == "Scale":
                fact.update(value="1 : 50", snippet="Scale: 1 : 50")
        extracted["sourceManifest"][0]["text"] = extracted["sourceManifest"][0]["text"].replace("Scale: 1 : 100", "Scale: 1 : 50")
        case.item.extracted_parameters_json = extracted
        case.db.commit()
        case.on_translate.append(newer_decision)
    else:
        case.gateway.on_upload = newer_decision
    assert worker.run_specifications_once(case.db, gateway=case.gateway,
        client=case.client, storage=case.storage) == ["superseded"]
    assert current(case).current_revision == before_revision
    case.gateway.on_upload = None
    # A save schedules the replacement immediately, preserving the newer generation.
    _, artifact = _publish(case)
    assert artifact.source_json["review"]["field_provenance"]["Decking"]["display_value"] == "Latest correction"


def test_relevant_sources_reopen_completion_without_discarding_saved_decisions(review_case):
    case = review_case
    _save(case, {label: {"operation": "specify_blank"} for label in FIELD_NAMES},
        selections={"selected_request_type": "new_enquiry"})
    specification, artifact = _publish(case)
    review = case.db.get(PerformanceSpecificationReview, (BOARD, ITEM), populate_existing=True)
    source = ReviewSourceSnapshot.model_validate(artifact.source_json["reviewSource"])
    command = reviews.ApproveReview(
        expected_review_version=review.review_version,
        expected_source_fingerprint=review.source_fingerprint,
        idempotency_key=uuid.uuid4().hex, artifact_revision=artifact.revision,
        expected_decision_digest=review.decision_digest,
        expected_publication_identity=review.desired_publication_identity,
    )
    case.db.commit()
    approved = reviews.approve_review(case.db, board_id=BOARD, item_id=ITEM,
        actor=ACTOR, command=command, source=source)
    before = reviews.review_state(case.db.get(PerformanceSpecificationReview, (BOARD, ITEM)))
    extracted = deepcopy(case.item.extracted_parameters_json)
    extracted["sourceManifest"][0]["contentSha256"] = "changed-email-content"
    case.item.extracted_parameters_json = extracted
    case.db.commit()
    assert run(case) == ["published"]
    review = case.db.get(PerformanceSpecificationReview, (BOARD, ITEM), populate_existing=True)
    assert review.current_approval_id is None
    assert reviews.review_state(review) == before
    assert case.db.get(PerformanceSpecificationApproval, uuid.UUID(approved.approval_id)) is not None
    assert case.db.query(PerformanceSpecificationReviewEvent).filter_by(
        board_id=BOARD, item_id=ITEM, command_type="approval_invalidated").count() == 1
    metadata = current(case).source_json["review"]
    assert all(field["requires_reconfirmation"] for field in metadata["field_provenance"].values())


def test_new_live_input_invalidates_completion_while_replacement_extraction_is_pending(review_case):
    case = review_case
    _save(case, {label: {"operation": "specify_blank"} for label in FIELD_NAMES},
        selections={"selected_request_type": "new_enquiry"})
    _, artifact = _publish(case)
    review = case.db.get(PerformanceSpecificationReview, (BOARD, ITEM), populate_existing=True)
    source = ReviewSourceSnapshot.model_validate(artifact.source_json["reviewSource"])
    command = reviews.ApproveReview(
        expected_review_version=review.review_version,
        expected_source_fingerprint=review.source_fingerprint,
        idempotency_key=uuid.uuid4().hex, artifact_revision=artifact.revision,
        expected_decision_digest=review.decision_digest,
        expected_publication_identity=review.desired_publication_identity,
    )
    case.db.commit()
    approved = reviews.approve_review(case.db, board_id=BOARD, item_id=ITEM,
        actor=ACTOR, command=command, source=source)
    before = reviews.review_state(case.db.get(PerformanceSpecificationReview, (BOARD, ITEM)))
    old_source_identity = deepcopy(review.source_identity_json)
    previous_uploads = len(case.gateway.uploads)
    case.gateway.target.input_revision = "new-email-awaiting-extraction"

    for _ in range(2):
        assert run(case) == ["failed"]
        review = case.db.get(PerformanceSpecificationReview, (BOARD, ITEM), populate_existing=True)
        assert review.current_approval_id is None
        assert reviews.review_state(review) == before
        # The worker must not relabel the previous extraction as the new request.
        assert review.source_identity_json == old_source_identity
        assert current(case).current_revision == artifact.revision
        assert len(case.gateway.uploads) == previous_uploads

    historical = case.db.get(PerformanceSpecificationApproval, uuid.UUID(approved.approval_id))
    assert historical is not None and historical.artifact_revision == artifact.revision
    assert case.db.query(PerformanceSpecificationReviewEvent).filter_by(
        board_id=BOARD, item_id=ITEM, command_type="approval_invalidated").count() == 1

