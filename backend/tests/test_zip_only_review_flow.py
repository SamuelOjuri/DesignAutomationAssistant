"""Files-only extraction remains useful while client request context needs review."""

from copy import deepcopy
import hashlib

import pytest

from backend.app.models import PerformanceSpecificationArtifact
from backend.app.services import specification_review as service
from backend.app.services.performance_specification_contract import SOURCE_REVIEW_DETAILS, source_review_notes
from backend.app.services.performance_specification_evidence import resolve_email_evidence
from backend.app.services.performance_specification_sources import resolve_specification_sources
from backend.app.services.specification_intent import build_request_intent
from backend.app.services.specification_review_contract import ReviewSourceSnapshot
from backend.app.services.specification_semantics import MISSING_CLIENT_EMAIL_REVIEW_STATUS
from backend.tests.test_performance_specification import BOARD, ITEM, case, current, pdf_text, run
from backend.tests.test_specification_evidence import candidate, extraction, source
from backend.tests.test_specification_review_persistence import (
    ACCOUNT, ACTOR, FIELD_NAMES, approve, persisted, publish_current_review,
    review_db, save, save_command, source_snapshot,
)


MISSING_EMAIL_ID = "request:missing-client-email"


def missing_email():
    return {
        "sourceId": MISSING_EMAIL_ID, "filename": "Client email", "sourceType": "request_context",
        "status": MISSING_CLIENT_EMAIL_REVIEW_STATUS, "text": "",
        "contentSha256": hashlib.sha256(b"").hexdigest(), "sizeBytes": 0, "observedAt": None,
    }


def files_only_case(case):
    payload = deepcopy(case.item.extracted_parameters_json)
    excluded = {"Company", "Contact", "Date Received", "Bauder Roof Num"}
    for label in excluded:
        payload["performanceParameters"][label] = None
        payload["parameters"][label] = None
    payload["specificationEvidence"] = [entry for entry in payload["specificationEvidence"] if entry["field"] not in excluded]
    for entry in payload["specificationEvidence"]:
        entry.update(source_id="asset:1:member:0", request_role="document", message_date=None)
    drawing = payload["sourceManifest"][0]
    drawing.update(sourceId="asset:1:member:0", filename="roof-package.zip!/roof-plan.pdf",
                   sourceType="attachment", observedAt=None)
    payload["sourceManifest"] = [drawing, missing_email()]
    payload["specificationRequestIntent"] = {
        "recommended_type": "uncertain", "explanation": "No client email was provided.",
        "ambiguity_reasons": ["The reviewer must confirm the request type."],
    }
    case.item.extracted_parameters_json = payload
    case.db.commit()


def test_files_only_source_has_actionable_warning_and_no_automatic_client_context(case):
    files_only_case(case)
    resolved = resolve_specification_sources(case.gateway, case.item)
    snapshot = ReviewSourceSnapshot.model_validate(resolved["reviewSource"])
    issue, = snapshot.source_issues
    assert issue.source_id == MISSING_EMAIL_ID and issue.code == MISSING_CLIENT_EMAIL_REVIEW_STATUS
    assert "No client email was provided" in issue.detail
    assert "supplied files only" in issue.detail and "File upload dates are not client request dates" in issue.detail
    assert source_review_notes(resolved["sourceManifest"]) == [issue.detail]
    assert snapshot.intent.recommended_type == "uncertain" and snapshot.request_type is None
    assert resolved["requestContext"] is None
    assert resolved["raw"]["Scale"] == "1 : 100"
    for label in ("Company", "Contact", "Date Received"):
        assert resolved["raw"][label] is None
        assert snapshot.fields[label].proposal.source == "unavailable"
        assert snapshot.fields[label].proposal.email_evidence == []


def test_files_only_worker_renders_review_warning_without_inventing_requester_or_date(case):
    files_only_case(case)
    assert run(case) == ["published"]
    record = current(case)
    assert record.last_error is None and record.review_json["status"] == "published_with_review"
    note, = record.review_json["sourceReviews"]
    assert "No client email was provided" in note
    artifact = case.db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, record.current_revision))
    assert artifact.source_json["requestContext"] is None
    assert artifact.translation_json["sourceReviews"] == [note]
    text = pdf_text(case.gateway.uploads[-1][1])
    assert "No client email was provided" in text and "supplied files only" in text
    assert "Alex Example" not in text and "Acme, Limited" not in text
    assert "29 Oct 2026" not in text and "30 Oct 2026" not in text


@pytest.mark.parametrize("label,value", [("Company", "Drawing Issuer Ltd"), ("Contact", "Designer Example"),
                                         ("Date Received", "12 Nov 2026")])
def test_missing_client_marker_excludes_document_client_claims(label, value):
    text = f"{label}: {value}"
    fact = candidate(label, value, text, source_id="asset:1:member:0", request_role="direct_request")
    payload = extraction([source("asset:1:member:0", text), missing_email()], [fact])
    raw, provenance, _ = resolve_email_evidence(payload)
    assert raw[label] is None and provenance[label]["selected"] is None
    assert provenance[label]["excludedCandidates"][0]["value"] == value
    assert "No client email was provided" in provenance[label]["excludedCandidates"][0]["reason"]


def test_files_without_client_email_cannot_establish_request_intent():
    manifest = [source("asset:1:member:0", "New enquiry for Example Campus"), missing_email()]
    proposal = {"recommended_type": "new_enquiry", "explanation": "The document says new enquiry.",
                "evidence": [{"source_id": "asset:1:member:0", "excerpt": "New enquiry for Example Campus",
                              "location": "cover"}]}
    intent = build_request_intent(manifest, proposal)
    assert intent.recommended_type == "uncertain"
    assert any("external client request" in reason for reason in intent.ambiguity_reasons)


def test_missing_email_issue_requires_current_audited_disposition_before_completion(review_db):
    payload = source_snapshot("files-only-source").model_dump(mode="json")
    payload["source_issues"] = [{"source_id": MISSING_EMAIL_ID, "code": MISSING_CLIENT_EMAIL_REVIEW_STATUS,
                                "detail": SOURCE_REVIEW_DETAILS[MISSING_CLIENT_EMAIL_REVIEW_STATUS]}]
    snapshot = ReviewSourceSnapshot.model_validate(payload)
    service.synchronize_review_source(review_db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT,
        source=snapshot, expected_source_fingerprint="sources-1")
    save(review_db, save_command(fingerprint="files-only-source", selections={"selected_request_type": "new_enquiry"},
        field_operations={label: {"operation": "accept_recommendation",
                                 "proposal_id": snapshot.fields[label].proposal.proposal_id} for label in FIELD_NAMES}),
        source=snapshot)
    command = publish_current_review(review_db, snapshot)
    with pytest.raises(service.ReviewConflict) as caught:
        approve(review_db, command, snapshot)
    assert caught.value.code == "source_unavailable" and persisted(review_db)["approval_id"] is None

    explanation = "Reviewed the supplied ZIP and confirmed the requester, request date and requested work separately."
    save(review_db, save_command(version=command.expected_review_version, fingerprint="files-only-source",
        source_dispositions=[{"source_id": MISSING_EMAIL_ID, "issue_code": MISSING_CLIENT_EMAIL_REVIEW_STATUS,
                              "explanation": explanation}]), source=snapshot)
    disposition, = persisted(review_db)["dispositions"]
    assert disposition["audit"]["reviewed_source"] == snapshot.identity.model_dump(mode="json")
    assert disposition["audit"]["actor"] == ACTOR.model_dump(mode="json")
    command = publish_current_review(review_db, snapshot)
    assert approve(review_db, command, snapshot).approval_id is not None
