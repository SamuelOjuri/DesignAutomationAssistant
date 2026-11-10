"""Fast review persistence checks; PostgreSQL locking is tested separately."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    DesignProcessingItem,
    PerformanceSpecification,
    PerformanceSpecificationArtifact,
    PerformanceSpecificationApproval,
    PerformanceSpecificationReview,
    PerformanceSpecificationReviewEvent,
)
from backend.app.services import specification_review as service
from backend.app.services.specification_review_artifacts import build_review_artifact_metadata
from backend.app.services.specification_review_contract import (
    FIELD_NAMES,
    ReviewActor,
    ReviewDecisionState,
    ReviewSourceSnapshot,
    SaveReviewDecisions,
    SavedFieldDecision,
    effective_field_value,
)


BOARD = "1882196103"
ITEM = "123"
ACCOUNT = "200"
ACTOR = ReviewActor(app_user_id="reviewer-1", monday_user_id="100", monday_account_id=ACCOUNT)


def source_snapshot(fingerprint="sources-1"):
    citation = {"source_id": "email:body", "excerpt": "Current client request", "location": "email body"}
    fields = {}
    for label in FIELD_NAMES:
        proposal = {
            "proposal_id": f"{fingerprint}:{label}",
            "display_value": f"Requested {label}",
            "source": "email_context",
            "rationale": "Selected client request",
            "email_evidence": [citation],
            "cad_mapping": {"status": "unmapped"},
        }
        if label in {"Drawing Reference", "Revision", "Incoming Information"}:
            proposal.update(
                display_value="" if label == "Incoming Information" else None,
                source="default_blank" if label == "Incoming Information" else "unassigned",
                email_evidence=[],
                cad_mapping={"status": "not_applicable"},
            )
        fields[label] = {"proposal": proposal, "applicability": "applicable"}
    return ReviewSourceSnapshot.model_validate({
        "identity": {
            "source_fingerprint": fingerprint,
            "extraction_identity": f"extraction:{fingerprint}",
            "source_policy_version": "specification-review-source-v1",
            "reference_parser_version": "test-parser-v1",
        },
        "intent": {"recommended_type": "new_enquiry", "explanation": "New scheme requested", "evidence": [citation]},
        "request_type": "new_enquiry",
        "candidates": {"status": "available"},
        "fields": fields,
    })


def save_command(version=0, fingerprint="sources-1", key=None, **changes):
    return SaveReviewDecisions.model_validate({
        "expected_review_version": version,
        "expected_source_fingerprint": fingerprint,
        "idempotency_key": key or uuid.uuid4().hex,
        **changes,
    })


@pytest.fixture()
def review_db():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine, tables=[
        DesignProcessingItem.__table__, PerformanceSpecification.__table__,
        PerformanceSpecificationArtifact.__table__, PerformanceSpecificationReview.__table__,
        PerformanceSpecificationReviewEvent.__table__, PerformanceSpecificationApproval.__table__,
    ])
    Session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with Session() as db:
        db.add(DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id=ITEM, state="ready_for_review"))
        db.flush()
        db.add(PerformanceSpecification(board_id=BOARD, item_id=ITEM,
            requested_generation=0, completed_generation=0))
        db.commit()
        service.initialize_review(db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT, source=source_snapshot())
        yield db
    engine.dispose()


def save(db, command, *, actor=ACTOR, source=None):
    return service.save_review_decisions(db, board_id=BOARD, item_id=ITEM,
        actor=actor, command=command, source=source or source_snapshot())


def persisted(db):
    review = db.get(PerformanceSpecificationReview, (BOARD, ITEM))
    specification = db.get(PerformanceSpecification, (BOARD, ITEM))
    events = db.query(PerformanceSpecificationReviewEvent).filter_by(board_id=BOARD, item_id=ITEM).all()
    data = {
        "version": review.review_version,
        "decisions": deepcopy(review.field_decisions_json),
        "dispositions": deepcopy(review.source_dispositions_json),
        "project": (review.selected_project_board_id, review.selected_project_item_id),
        "drawing": (review.selected_drawing_board_id, review.selected_drawing_item_id),
        "request_type": review.selected_request_type,
        "source_fingerprint": review.source_fingerprint,
        "approval_id": review.current_approval_id,
        "generation": specification.requested_generation,
        "scheduled": specification.scheduled_for,
        "event_count": len(events),
    }
    db.rollback()  # Read snapshots must not leave a transaction open before a command.
    return data


@pytest.mark.parametrize("label", FIELD_NAMES)
def test_every_pdf_field_persists_literal_blank_and_restore(review_db, label):
    literal = "  R\u00e9viewer custom text\nsecond line  "
    first = save(review_db, save_command(field_operations={label: {
        "operation": "specify_value", "display_value": literal,
    }}))
    state = persisted(review_db)
    saved = SavedFieldDecision.model_validate(state["decisions"][label])
    assert saved.decision.display_value == literal
    assert saved.audit.actor == ACTOR
    assert saved.audit.reviewed_source == source_snapshot().identity
    expected_mapping = "mapped" if label in {"Drawing Title", "Bauder Contract Number", "Bauder Roof Num"} else "unmapped"
    assert saved.decision.cad_mapping.status == expected_mapping
    assert first.review_version == state["version"] == 1
    assert state["generation"] >= 1 and state["scheduled"] is not None

    save(review_db, save_command(version=1, field_operations={label: {"operation": "specify_blank"}}))
    state = persisted(review_db)
    blank = SavedFieldDecision.model_validate(state["decisions"][label])
    effective = effective_field_value(source_snapshot().fields[label].proposal,
        blank, current_source=source_snapshot().identity)
    assert effective.display_value == ""
    assert effective.source_label == "Reviewer Specified"
    assert state["version"] == 2

    save(review_db, save_command(version=2, field_operations={label: {"operation": "restore_recommendation"}}))
    state = persisted(review_db)
    assert label not in state["decisions"]
    assert state["version"] == 3


def test_acceptance_snapshots_proposal_and_retains_email_provenance(review_db):
    source = source_snapshot()
    result = save(review_db, save_command(field_operations={"Decking": {
        "operation": "accept_recommendation", "proposal_id": source.fields["Decking"].proposal.proposal_id,
    }}))
    state = persisted(review_db)
    saved = SavedFieldDecision.model_validate(state["decisions"]["Decking"])
    assert saved.decision.snapshot == source.fields["Decking"].proposal
    assert saved.decision.snapshot.source == "email_context"
    assert result.review_version == 1


def test_same_text_explicit_entry_is_distinct_from_acceptance(review_db):
    source = source_snapshot()
    save(review_db, save_command(field_operations={"Decking": {
        "operation": "accept_recommendation", "proposal_id": source.fields["Decking"].proposal.proposal_id,
    }}))
    save(review_db, save_command(version=1, field_operations={"Decking": {
        "operation": "specify_value", "display_value": source.fields["Decking"].proposal.display_value,
    }}))
    state = persisted(review_db)
    assert state["version"] == 2
    saved = SavedFieldDecision.model_validate(state["decisions"]["Decking"])
    assert effective_field_value(source.fields["Decking"].proposal, saved,
        current_source=source.identity).source_label == "Reviewer Specified"


def test_identical_retry_returns_original_result_after_later_edits(review_db):
    command = save_command(key="retry-key", field_operations={"Decking": {"operation": "specify_blank"}})
    original = save(review_db, command)
    save(review_db, save_command(version=1, field_operations={"Contact": {"operation": "specify_blank"}}))
    before = persisted(review_db)
    retry = save(review_db, command)
    after = persisted(review_db)
    assert retry == original
    assert after == before
    assert after["version"] == 2


def test_idempotency_key_reuse_with_different_request_is_conflict(review_db):
    save(review_db, save_command(key="same-key", field_operations={"Decking": {"operation": "specify_blank"}}))
    before = persisted(review_db)
    with pytest.raises(service.ReviewConflict):
        save(review_db, save_command(version=1, key="same-key", field_operations={"Contact": {"operation": "specify_blank"}}))
    assert persisted(review_db) == before


@pytest.mark.parametrize("changes,code", [
    ({"version": 9}, "stale_review"),
    ({"fingerprint": "outdated-source"}, "source_changed"),
])
def test_stale_save_cannot_change_decisions_or_queue(review_db, changes, code):
    before = persisted(review_db)
    with pytest.raises(service.ReviewConflict) as caught:
        save(review_db, save_command(**changes, field_operations={"Decking": {"operation": "specify_blank"}}))
    assert caught.value.code == code
    assert persisted(review_db) == before


def test_noop_does_not_increment_review_or_queue(review_db):
    save(review_db, save_command(field_operations={"Decking": {"operation": "specify_blank"}}))
    before = persisted(review_db)
    result = save(review_db, save_command(version=1, field_operations={"Decking": {"operation": "specify_blank"}}))
    after = persisted(review_db)
    assert not result.changed
    assert after["version"] == before["version"]
    assert after["generation"] == before["generation"]
    assert after["decisions"] == before["decisions"]
    assert after["event_count"] == before["event_count"] + 1  # Retains the retry receipt.
    receipt = review_db.get(PerformanceSpecificationReviewEvent, uuid.UUID(result.event_id))
    assert receipt.replacement_decisions_json == receipt.original_decisions_json
    assert receipt.replacement_decisions_json["field_decisions"] == after["decisions"]
    review_db.rollback()


def test_invalid_proposal_rolls_back_valid_operations_in_same_command(review_db):
    before = persisted(review_db)
    with pytest.raises(service.ReviewConflict):
        save(review_db, save_command(field_operations={
            "Contact": {"operation": "specify_blank"},
            "Decking": {"operation": "accept_recommendation", "proposal_id": "obsolete-proposal"},
        }))
    assert persisted(review_db) == before


def test_actor_from_another_account_cannot_read_retry_receipt_or_save(review_db):
    command = save_command(key="private-receipt", field_operations={"Decking": {"operation": "specify_blank"}})
    save(review_db, command)
    before = persisted(review_db)
    stranger = ACTOR.model_copy(update={"monday_account_id": "999"})
    with pytest.raises(service.ReviewConflict):
        save(review_db, command, actor=stranger)
    assert persisted(review_db) == before


def test_source_refresh_preserves_decisions_and_reopens_completion(review_db):
    save(review_db, save_command(field_operations={"Decking": {"operation": "specify_blank"}}))
    before = persisted(review_db)
    service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM,
        account_id=ACCOUNT, source=source_snapshot("sources-2"), expected_source_fingerprint="sources-1")
    after = persisted(review_db)
    assert after["decisions"] == before["decisions"]
    assert after["version"] == before["version"]
    assert after["source_fingerprint"] == "sources-2"
    assert after["generation"] > before["generation"]
    saved = SavedFieldDecision.model_validate(after["decisions"]["Decking"])
    current = source_snapshot("sources-2")
    effective = effective_field_value(current.fields["Decking"].proposal, saved, current_source=current.identity)
    assert effective.display_value == "" and effective.requires_reconfirmation


def test_source_dispositions_are_audited_and_independent_of_field_edits(review_db):
    source = source_snapshot().model_dump(mode="json")
    source["identity"]["source_fingerprint"] = "sources-with-issue"
    source["source_issues"] = [{"source_id": "attachment:1", "code": "unread_file", "detail": "Unreadable attachment"}]
    source = ReviewSourceSnapshot.model_validate(source)
    service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
        source=source, expected_source_fingerprint="sources-1")
    save(review_db, save_command(fingerprint="sources-with-issue", source_dispositions=[{
        "source_id": "attachment:1", "issue_code": "unread_file", "explanation": "Checked the attachment manually",
    }]), source=source)
    state = persisted(review_db)
    assert state["dispositions"][0]["explanation"] == "Checked the attachment manually"
    assert state["dispositions"][0]["audit"]["actor"] == ACTOR.model_dump()
    save(review_db, save_command(version=1, fingerprint="sources-with-issue",
        field_operations={"Decking": {"operation": "specify_blank"}}), source=source)
    assert persisted(review_db)["dispositions"] == state["dispositions"]


def test_selection_patch_distinguishes_omission_from_explicit_clear(review_db):
    source = source_snapshot("sources-with-candidate").model_dump(mode="json")
    project = {"board_id": "70002", "item_id": "81001"}
    source["candidates"]["projects"] = [{"record": project, "rationale": "Detected reference"}]
    source = ReviewSourceSnapshot.model_validate(source)
    service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
        source=source, expected_source_fingerprint="sources-1")
    save(review_db, save_command(fingerprint="sources-with-candidate", selections={"selected_project": project}), source=source)
    state = persisted(review_db)
    assert state["project"] == ("70002", "81001")

    save(review_db, save_command(version=1, fingerprint="sources-with-candidate",
        selections={"selected_request_type": "new_enquiry"}), source=source)
    state = persisted(review_db)
    assert state["project"] == ("70002", "81001")
    assert state["request_type"] == "new_enquiry"

    save(review_db, save_command(version=2, fingerprint="sources-with-candidate",
        selections={"selected_project": None}), source=source)
    state = persisted(review_db)
    assert state["project"] == (None, None)
    assert state["request_type"] == "new_enquiry"


def test_repeated_source_observation_does_not_requeue(review_db):
    before = persisted(review_db)
    service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM,
        account_id=ACCOUNT, source=source_snapshot())
    assert persisted(review_db) == before


def test_delayed_refresh_cannot_replace_a_newer_source_snapshot(review_db):
    service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
        source=source_snapshot("sources-2"), expected_source_fingerprint="sources-1")
    before = persisted(review_db)
    with pytest.raises(service.ReviewConflict) as caught:
        service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
            source=source_snapshot("delayed-sources"), expected_source_fingerprint="sources-1")
    assert caught.value.code == "source_changed"
    assert persisted(review_db) == before


def test_retry_normalizes_explicit_default_command_members(review_db):
    original = save_command(key="normalized-retry", field_operations={"Decking": {"operation": "specify_blank"}})
    result = save(review_db, original)
    repeated = SaveReviewDecisions.model_validate({
        **original.model_dump(mode="json"), "schema_version": 1, "explanation": None,
        "source_dispositions": [], "selections": None,
    })
    before = persisted(review_db)
    assert save(review_db, repeated) == result
    assert persisted(review_db) == before


def test_command_refuses_to_commit_an_unrelated_open_transaction(review_db):
    review_db.get(PerformanceSpecification, (BOARD, ITEM))
    with pytest.raises(RuntimeError):
        save(review_db, save_command(field_operations={"Decking": {"operation": "specify_blank"}}))
    review_db.rollback()
    assert persisted(review_db)["version"] == 0


def published_review(db):
    """Seed the exact output the Phase 4 publisher will persist."""
    source = source_snapshot()
    selected = save(db, save_command(selections={"selected_request_type": "new_enquiry"}))
    save(db, save_command(version=selected.review_version, field_operations={
        label: {"operation": "accept_recommendation", "proposal_id": source.fields[label].proposal.proposal_id}
        for label in FIELD_NAMES
    }))
    return source, publish_current_review(db, source)


def publish_current_review(db, source):
    """Simulate publication after a save/refresh without invoking remote services."""
    record = db.get(PerformanceSpecificationReview, (BOARD, ITEM))
    metadata = build_review_artifact_metadata(service.review_state(record), source)
    artifact = PerformanceSpecificationArtifact(
        board_id=BOARD, item_id=ITEM, revision=f"review-pdf-{uuid.uuid4().hex}",
        translation_json={}, source_json={"review": metadata}, model_version="review-test-v1",
        filename="review.pdf", storage_bucket="private", storage_object_key="review.pdf",
        content_sha256="a" * 64, size_bytes=123, monday_asset_id="901", status="published",
    )
    db.add(artifact)
    specification = db.get(PerformanceSpecification, (BOARD, ITEM))
    specification.current_revision = artifact.revision
    specification.completed_generation = specification.requested_generation
    specification.scheduled_for = None
    specification.lease_token = specification.lease_until = None
    db.commit()
    return service.ApproveReview(
        expected_review_version=record.review_version,
        expected_source_fingerprint=source.identity.source_fingerprint,
        idempotency_key=uuid.uuid4().hex,
        artifact_revision=artifact.revision,
        expected_decision_digest=record.decision_digest,
        expected_publication_identity=record.desired_publication_identity,
    )


def approve(db, command, source):
    return service.approve_review(db, board_id=BOARD, item_id=ITEM,
        actor=ACTOR, command=command, source=source)


def test_approval_is_exact_immutable_history_and_does_not_queue_another_pdf(review_db):
    source, command = published_review(review_db)
    before = persisted(review_db)
    result = approve(review_db, command, source)
    after = persisted(review_db)
    assert result.approval_id is not None
    assert str(after["approval_id"]) == str(result.approval_id)
    assert after["version"] == before["version"]
    assert after["generation"] == before["generation"]
    assert approve(review_db, command, source) == result
    assert persisted(review_db) == after
    approval = review_db.get(PerformanceSpecificationApproval, uuid.UUID(str(result.approval_id)))
    assert approval.artifact_revision == command.artifact_revision
    assert approval.source_fingerprint == command.expected_source_fingerprint
    assert approval.decision_digest == command.expected_decision_digest
    assert approval.actor_monday_user_id == ACTOR.monday_user_id
    review_db.rollback()


def test_approving_a_different_pdf_replaces_only_current_pointer_and_retains_both_approvals(review_db):
    source, first_command = published_review(review_db)
    first = approve(review_db, first_command, source)
    first_approval = review_db.get(PerformanceSpecificationApproval, uuid.UUID(first.approval_id))
    original = {column.name: getattr(first_approval, column.name) for column in PerformanceSpecificationApproval.__table__.columns}
    review_db.rollback()
    before = persisted(review_db)

    second_command = publish_current_review(review_db, source)
    assert second_command.artifact_revision != first_command.artifact_revision
    assert second_command.expected_review_version == first_command.expected_review_version
    assert second_command.expected_publication_identity == first_command.expected_publication_identity
    second = approve(review_db, second_command, source)
    after = persisted(review_db)
    assert second.changed
    assert second.approval_id != first.approval_id
    assert str(after["approval_id"]) == second.approval_id
    assert after["generation"] == before["generation"]
    assert after["version"] == before["version"]

    approvals = review_db.query(PerformanceSpecificationApproval).all()
    assert len(approvals) == 2
    prior = next(approval for approval in approvals if str(approval.id) == first.approval_id)
    assert {column.name: getattr(prior, column.name) for column in PerformanceSpecificationApproval.__table__.columns} == original
    replacement = next(approval for approval in approvals if str(approval.id) == second.approval_id)
    assert replacement.artifact_revision == second_command.artifact_revision
    events = review_db.query(PerformanceSpecificationReviewEvent).filter_by(command_type="approval_invalidated").all()
    assert len(events) == 1
    assert first.approval_id in events[0].explanation
    review_db.rollback()


@pytest.mark.parametrize("change", [
    "historic_pdf", "private_pdf", "missing_asset", "newer_generation", "live_lease", "wrong_metadata",
])
def test_approval_rejects_any_pdf_that_is_not_current_published_output(review_db, change):
    source, command = published_review(review_db)
    artifact = review_db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, command.artifact_revision))
    specification = review_db.get(PerformanceSpecification, (BOARD, ITEM))
    if change == "historic_pdf":
        artifact.source_json = None
    elif change == "private_pdf":
        artifact.status = "rendered"
    elif change == "missing_asset":
        artifact.monday_asset_id = None
    elif change == "newer_generation":
        specification.requested_generation += 1
    elif change == "live_lease":
        specification.lease_token = "worker-1"
        specification.lease_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    else:
        altered = deepcopy(artifact.source_json)
        altered["review"]["review_version"] += 1
        artifact.source_json = altered
    review_db.commit()
    before = persisted(review_db)
    with pytest.raises(service.ReviewConflict):
        approve(review_db, command, source)
    after = persisted(review_db)
    assert after == before
    assert after["approval_id"] is None
    assert review_db.query(PerformanceSpecificationApproval).count() == 0
    review_db.rollback()


def test_source_change_clears_current_approval_but_preserves_history_and_overrides(review_db):
    source, command = published_review(review_db)
    approved = approve(review_db, command, source)
    before = persisted(review_db)
    service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM,
        account_id=ACCOUNT, source=source_snapshot("sources-2"), expected_source_fingerprint="sources-1")
    after = persisted(review_db)
    assert after["approval_id"] is None
    assert after["decisions"] == before["decisions"]
    assert after["generation"] > before["generation"]
    assert review_db.get(PerformanceSpecificationApproval, uuid.UUID(str(approved.approval_id))) is not None
    review_db.rollback()


@pytest.mark.parametrize("problem,code", [
    ("missing_request_type", "source_changed"),
    ("unaccepted_field", "stale_review"),
    ("stale_acceptance", "source_changed"),
    ("unaddressed_source_issue", "source_unavailable"),
    ("unavailable_field", "source_unavailable"),
])
def test_current_pdf_cannot_be_approved_with_outstanding_review_work(review_db, problem, code):
    source, command = published_review(review_db)
    if problem == "missing_request_type":
        save(review_db, save_command(version=command.expected_review_version,
            selections={"selected_request_type": None}))
    elif problem == "unaccepted_field":
        save(review_db, save_command(version=command.expected_review_version,
            field_operations={"Decking": {"operation": "restore_recommendation"}}))
    else:
        payload = source_snapshot("sources-2").model_dump(mode="json")
        if problem == "unaddressed_source_issue":
            payload["source_issues"] = [{"source_id": "attachment:1", "code": "unread_file", "detail": "Unreadable attachment"}]
        if problem == "unavailable_field":
            payload["fields"]["Decking"]["proposal"] = {
                "proposal_id": "unavailable-decking", "source": "unavailable", "display_value": None,
                "rationale": "No reliable decking source", "cad_mapping": {"status": "not_applicable"},
            }
        source = ReviewSourceSnapshot.model_validate(payload)
        service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
            source=source, expected_source_fingerprint="sources-1")
        if problem != "stale_acceptance":
            operations = {label: {"operation": "accept_recommendation", "proposal_id": source.fields[label].proposal.proposal_id}
                for label in FIELD_NAMES if source.fields[label].proposal.source != "unavailable"}
            if problem == "unavailable_field":
                operations["Decking"] = {"operation": "restore_recommendation"}
            save(review_db, save_command(version=command.expected_review_version,
                fingerprint="sources-2", field_operations=operations), source=source)
    current_command = publish_current_review(review_db, source)
    with pytest.raises(service.ReviewConflict) as caught:
        approve(review_db, current_command, source)
    assert caught.value.code == code
    assert persisted(review_db)["approval_id"] is None


def manual_state(operation="specify_blank"):
    source = source_snapshot()
    decision = {"operation": operation}
    if operation == "specify_value":
        decision["display_value"] = "  Exact custom text\nsecond line  "
    saved = SavedFieldDecision.model_validate({
        "decision": decision,
        "audit": {
            "actor": ACTOR.model_dump(), "saved_at": "2026-11-09T10:00:00Z",
            "reviewed_source": source.identity.model_dump(),
        },
    })
    return ReviewDecisionState(review_version=3, field_decisions={label: saved.model_copy(deep=True) for label in FIELD_NAMES})


def test_artifact_identity_excludes_actor_and_polling_or_audit_timestamps():
    source = source_snapshot()
    state = manual_state()
    changed_audit = state.model_copy(deep=True)
    for saved in changed_audit.field_decisions.values():
        saved.audit.actor = ACTOR.model_copy(update={"app_user_id": "another-reviewer", "monday_user_id": "101"})
        saved.audit.saved_at = datetime(2026, 11, 9, 11, tzinfo=timezone.utc)
    assert build_review_artifact_metadata(state, source) == build_review_artifact_metadata(changed_audit, source)


@pytest.mark.parametrize("operation", ["specify_value", "specify_blank"])
def test_artifact_records_all_18_manual_sources_without_fabricated_email_evidence(operation):
    metadata = build_review_artifact_metadata(manual_state(operation), source_snapshot())
    assert tuple(metadata["field_provenance"]) == FIELD_NAMES
    for field in metadata["field_provenance"].values():
        assert field["source"] == "reviewer_specified"
        assert field["source_label"] == "Reviewer Specified"
        assert field["display_value"] == ("" if operation == "specify_blank" else "  Exact custom text\nsecond line  ")
        assert field["email_evidence"] == field["crm_evidence"] == []
        assert field["derived_from"] is None
        assert field["cad_mapping"]["status"] == ("not_applicable" if operation == "specify_blank" else "unmapped")


def test_same_display_text_acceptance_and_explicit_entry_have_different_artifact_identities():
    source = source_snapshot()
    state = manual_state()
    audit = state.field_decisions["Decking"].audit
    accepted = state.model_copy(deep=True)
    accepted.field_decisions["Decking"] = SavedFieldDecision.model_validate({
        "decision": {"operation": "accept_recommendation", "snapshot": source.fields["Decking"].proposal.model_dump()},
        "audit": audit.model_dump(),
    })
    specified = state.model_copy(deep=True)
    specified.field_decisions["Decking"] = SavedFieldDecision.model_validate({
        "decision": {"operation": "specify_value", "display_value": source.fields["Decking"].proposal.display_value},
        "audit": audit.model_dump(),
    })
    automatic = build_review_artifact_metadata(accepted, source)
    manual = build_review_artifact_metadata(specified, source)
    assert automatic["field_provenance"]["Decking"]["display_value"] == manual["field_provenance"]["Decking"]["display_value"]
    assert automatic["decision_digest"] != manual["decision_digest"]
    assert automatic["publication_identity"] != manual["publication_identity"]
    assert automatic["field_provenance"]["Decking"]["source_label"] == "Email Context"
    assert manual["field_provenance"]["Decking"]["source_label"] == "Reviewer Specified"


def amendment_roof_snapshot(roof="a"):
    payload = source_snapshot(f"amendment-roof-{roof}").model_dump(mode="json")
    project = {"board_id": "70002", "item_id": "81001"}
    drawings = {"a": {"board_id": "70003", "item_id": "82001"},
        "b": {"board_id": "70003", "item_id": "82002"}}
    references = {"a": "AB12345_26.01 - A", "b": "AB12345_26.02 - B"}
    payload["intent"]["recommended_type"] = payload["request_type"] = "amendment"
    payload["selected_project"] = project
    payload["selected_drawing"] = drawings[roof]
    payload["candidates"] = {
        "status": "available", "projects": [{"record": project, "rationale": "Referenced project"}],
        "drawings": [{"record": record, "project": project, "literal_reference": references[key],
            "roof_scope": f"Roof {key.upper()}", "lifecycle": "active", "match_kind": "other",
            "rationale": "Plausible roof drawing"} for key, record in drawings.items()],
    }
    citation = {"source_id": "email:body", "excerpt": "Amend the existing scheme", "location": "client email"}
    for label, value in {"Drawing Reference": references[roof], "Revision": references[roof].split("_", 1)[1]}.items():
        payload["fields"][label]["proposal"].update(display_value=value,
            source="derived" if label == "Revision" else "email_context", email_evidence=[citation],
            cad_mapping={"status": "unmapped"})
    payload["fields"]["Revision"]["proposal"]["derived_from"] = "Drawing Reference"
    payload["fields"]["Company"]["proposal"].update(source="monday_crm", email_evidence=[],
        crm_evidence=[{"record": project, "column_id": "account"}])
    payload["fields"]["Decking"]["proposal"]["display_value"] = f"Decking for roof {roof.upper()}"
    return ReviewSourceSnapshot.model_validate(payload)


def test_changing_roof_requires_matching_fresh_recommendations_and_reconfirmation(review_db):
    first_source = amendment_roof_snapshot("a")
    service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
        source=first_source, expected_source_fingerprint="sources-1")
    selected = save(review_db, save_command(fingerprint=first_source.identity.source_fingerprint,
        selections={"selected_request_type": "amendment", "selected_project": first_source.selected_project.model_dump(),
            "selected_drawing": first_source.selected_drawing.model_dump()}), source=first_source)
    accepted = save(review_db, save_command(version=selected.review_version,
        fingerprint=first_source.identity.source_fingerprint, field_operations={
            label: {"operation": "accept_recommendation", "proposal_id": first_source.fields[label].proposal.proposal_id}
            for label in FIELD_NAMES}), source=first_source)
    second_source = amendment_roof_snapshot("b")
    changed = save(review_db, save_command(version=accepted.review_version,
        fingerprint=first_source.identity.source_fingerprint,
        selections={"selected_drawing": second_source.selected_drawing.model_dump()}), source=first_source)

    with pytest.raises(service.ReviewConflict) as caught:
        save(review_db, save_command(version=changed.review_version,
            fingerprint=first_source.identity.source_fingerprint, field_operations={"Decking": {
                "operation": "accept_recommendation", "proposal_id": first_source.fields["Decking"].proposal.proposal_id,
            }}), source=first_source)
    assert caught.value.code == "source_changed"

    stale_pdf = publish_current_review(review_db, first_source)
    with pytest.raises(service.ReviewConflict) as caught:
        approve(review_db, stale_pdf, first_source)
    assert caught.value.code == "source_changed"
    before_refresh = persisted(review_db)

    service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
        source=second_source, expected_source_fingerprint=first_source.identity.source_fingerprint)
    after_refresh = persisted(review_db)
    assert after_refresh["decisions"] == before_refresh["decisions"]
    assert after_refresh["decisions"]["Decking"]["decision"]["snapshot"]["display_value"] == "Decking for roof A"
    refreshed_pdf = publish_current_review(review_db, second_source)
    with pytest.raises(service.ReviewConflict) as caught:
        approve(review_db, refreshed_pdf, second_source)
    assert caught.value.code == "source_changed"

    save(review_db, save_command(version=changed.review_version,
        fingerprint=second_source.identity.source_fingerprint, field_operations={
            label: {"operation": "accept_recommendation", "proposal_id": second_source.fields[label].proposal.proposal_id}
            for label in FIELD_NAMES}), source=second_source)
    current_pdf = publish_current_review(review_db, second_source)
    approved = approve(review_db, current_pdf, second_source)
    assert approved.approval_id
    current = persisted(review_db)
    assert current["decisions"]["Decking"]["decision"]["snapshot"]["display_value"] == "Decking for roof B"
