"""Checkbox acknowledgements retain source-specific audit and completion gates."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.app.services import specification_review as service
from backend.app.services.specification_review_artifacts import decision_digest, review_digest
from backend.app.services.specification_review_contract import (
    FIELD_NAMES, ReviewDecisionState, ReviewSourceSnapshot, SaveReviewDecisions, SourceDisposition,
)
from backend.tests.test_specification_review_contract import AUDIT, command
from backend.tests.test_specification_review_persistence import (
    ACCOUNT, ACTOR, BOARD, ITEM, approve, persisted, publish_current_review,
    review_db, save, save_command, source_snapshot,
)


ISSUE = {"source_id": "attachment:1", "issue_code": "unread_file"}


def issue_source():
    payload = source_snapshot("sources-with-issue").model_dump(mode="json")
    payload["source_issues"] = [{"source_id": ISSUE["source_id"], "code": ISSUE["issue_code"],
                                 "detail": "Unreadable attachment"}]
    return ReviewSourceSnapshot.model_validate(payload)


def initialize_issue(db):
    source = issue_source()
    service.synchronize_review_source(db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
        source=source, expected_source_fingerprint="sources-1")
    return source


@pytest.mark.parametrize("addressed", [True, False])
def test_checkbox_commands_accept_explicit_boolean_without_prose(addressed):
    result = SaveReviewDecisions.model_validate(command(source_dispositions=[{**ISSUE, "addressed": addressed}]))
    disposition, = result.source_dispositions
    assert disposition.addressed is addressed and disposition.explanation is None


@pytest.mark.parametrize("changes", [{}, {"addressed": None}, {"addressed": "true"},
                                     {"addressed": 1}, {"explanation": "   "}])
def test_checkbox_commands_do_not_silently_acknowledge_missing_or_coerced_values(changes):
    with pytest.raises(ValidationError):
        SaveReviewDecisions.model_validate(command(source_dispositions=[{**ISSUE, **changes}]))


def test_legacy_notes_still_load_as_addressed_and_keep_their_original_decision_digest():
    note = {**ISSUE, "explanation": "Checked the original file manually"}
    legacy_command = SaveReviewDecisions.model_validate(command(source_dispositions=[note]))
    assert legacy_command.source_dispositions[0].addressed is True
    old_command_body = legacy_command.model_dump(mode="json")
    old_command_body["source_dispositions"][0].pop("addressed")
    assert service._command_digest("save_decisions", legacy_command) == review_digest({
        "command": "save_decisions", "body": old_command_body,
    })
    saved = SourceDisposition.model_validate({**note, "audit": AUDIT})
    assert saved.addressed is True and saved.explanation == note["explanation"]
    state = ReviewDecisionState(source_dispositions=[saved])
    historical_digest = review_digest({
        "schema_version": state.schema_version, "selections": state.selections.model_dump(mode="json"),
        "field_decisions": {}, "source_dispositions": [{**note, "reviewed_source": AUDIT["reviewed_source"]}],
    })
    assert decision_digest(state) == historical_digest
    reopened = state.model_copy(deep=True)
    reopened.source_dispositions[0].addressed = False
    assert decision_digest(reopened) != historical_digest


def test_checkbox_can_be_saved_and_revoked_without_losing_historical_notes(review_db):
    source = initialize_issue(review_db)
    save(review_db, save_command(fingerprint="sources-with-issue", source_dispositions=[{
        **ISSUE, "explanation": "Original manual review note",
    }]), source=source)
    before = persisted(review_db)
    save(review_db, save_command(version=1, fingerprint="sources-with-issue",
        source_dispositions=[{**ISSUE, "addressed": False}]), source=source)
    unchecked = persisted(review_db)
    saved, = unchecked["dispositions"]
    assert saved["addressed"] is False and saved["explanation"] == "Original manual review note"
    assert saved["audit"]["actor"] == ACTOR.model_dump()
    assert saved["audit"]["reviewed_source"] == source.identity.model_dump()
    assert unchecked["decisions"] == before["decisions"] == {}
    assert unchecked["version"] == before["version"] + 1
    assert unchecked["generation"] > before["generation"]
    save(review_db, save_command(version=2, fingerprint="sources-with-issue",
        source_dispositions=[{**ISSUE, "addressed": True}]), source=source)
    rechecked = persisted(review_db)["dispositions"][0]
    assert rechecked["addressed"] is True and rechecked["explanation"] == saved["explanation"]


@pytest.mark.parametrize("change", ["unchecked", "stale_source"])
def test_only_checked_acknowledgements_for_current_sources_allow_completion(review_db, change):
    source = initialize_issue(review_db)
    save(review_db, save_command(fingerprint="sources-with-issue",
        selections={"selected_request_type": "new_enquiry"},
        field_operations={label: {"operation": "accept_recommendation",
            "proposal_id": source.fields[label].proposal.proposal_id} for label in FIELD_NAMES},
        source_dispositions=[{**ISSUE, "addressed": True}]), source=source)
    first_pdf = publish_current_review(review_db, source)
    approval = approve(review_db, first_pdf, source)
    assert approval.approval_id is not None

    if change == "unchecked":
        save(review_db, save_command(version=1, fingerprint="sources-with-issue",
            source_dispositions=[{**ISSUE, "addressed": False}]), source=source)
    else:
        payload = source.model_dump(mode="json")
        payload["identity"]["source_fingerprint"] = "changed-source"
        payload["source_issues"][0]["detail"] = "Changed unreadable attachment"
        source = ReviewSourceSnapshot.model_validate(payload)
        service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
            source=source, expected_source_fingerprint="sources-with-issue")
        # Reconfirm field decisions only; the old checked source issue is stale.
        save(review_db, save_command(version=1, fingerprint="changed-source",
            field_operations={label: {"operation": "accept_recommendation",
                "proposal_id": source.fields[label].proposal.proposal_id} for label in FIELD_NAMES}), source=source)
    assert persisted(review_db)["approval_id"] is None
    latest_pdf = publish_current_review(review_db, source)
    before = deepcopy(persisted(review_db))
    with pytest.raises(service.ReviewConflict, match="Review outstanding source issues"):
        approve(review_db, latest_pdf, source)
    assert persisted(review_db) == before
