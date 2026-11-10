"""Transactional Specification Review persistence (no remote calls).

Public commands own one short transaction and require a clean Session. Callers
must authenticate the actor and perform fresh Monday access/scope/mode checks
before calling; the review API supplies this boundary for authenticated users.
The specification row is always locked before its review row, matching the
publisher's lock order. No human decisions live in processing review_json.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
import uuid

from pydantic import Field

from ..models import (
    PerformanceSpecification, PerformanceSpecificationArtifact,
    PerformanceSpecificationReview, PerformanceSpecificationReviewEvent,
    PerformanceSpecificationApproval,
)
from .specification_review_contract import (
    AcceptedRecommendation, DecisionAudit, NonemptyText, ReviewActor,
    ReviewDecisionState, ReviewModel, ReviewSelections, ReviewSourceSnapshot,
    SaveReviewDecisions, SavedFieldDecision, SourceDisposition,
    SpecifiedBlank, SpecifiedValue,
)
from .specification_review_artifacts import (
    build_review_artifact_metadata, decision_digest, review_digest, source_context_matches,
)


class ReviewConflict(RuntimeError):
    """Stable application errors for API conflict/access/pending responses."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


class ReviewCommandResult(ReviewModel):
    review_version: int
    source_fingerprint: str
    decision_digest: str
    desired_publication_identity: str
    approval_id: str | None = None
    changed: bool
    event_id: str | None = None


class ApproveReview(ReviewModel):
    expected_review_version: int = Field(ge=0, strict=True)
    expected_source_fingerprint: NonemptyText
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
    artifact_revision: NonemptyText
    expected_decision_digest: NonemptyText
    expected_publication_identity: NonemptyText
    explanation: str | None = Field(default=None, max_length=4000)


def _now():
    return datetime.now(timezone.utc)


def _clean_session(db):
    if db.in_transaction():
        raise RuntimeError("Review commands require a Session without an active transaction")


def _locked_rows(db, board_id, item_id):
    spec = db.query(PerformanceSpecification).filter_by(
        board_id=str(board_id), item_id=str(item_id),
    ).populate_existing().with_for_update().one_or_none()
    if spec is None:
        raise ReviewConflict("source_waiting", "No durable specification record exists")
    review = db.query(PerformanceSpecificationReview).filter_by(
        board_id=str(board_id), item_id=str(item_id),
    ).populate_existing().with_for_update().one_or_none()
    return spec, review


def _tenant(review, account_id):
    if review is None:
        raise ReviewConflict("source_waiting", "Review sources have not been initialized")
    if review.monday_account_id != str(account_id):
        raise ReviewConflict("reviewer_forbidden", "The review belongs to a different account")


def review_state(review) -> ReviewDecisionState:
    return ReviewDecisionState.model_validate({
        "schema_version": review.schema_version,
        "review_version": review.review_version,
        "selections": {
            "selected_request_type": review.selected_request_type,
            "selected_project": ({"board_id": review.selected_project_board_id, "item_id": review.selected_project_item_id}
                                 if review.selected_project_item_id else None),
            "selected_drawing": ({"board_id": review.selected_drawing_board_id, "item_id": review.selected_drawing_item_id}
                                 if review.selected_drawing_item_id else None),
        },
        "field_decisions": review.field_decisions_json or {},
        "source_dispositions": review.source_dispositions_json or [],
    })


def _write_state(review, state, source):
    review.schema_version = state.schema_version
    review.review_version = state.review_version
    selections = state.selections
    review.selected_request_type = selections.selected_request_type
    for kind in ("project", "drawing"):
        selected = getattr(selections, f"selected_{kind}")
        setattr(review, f"selected_{kind}_board_id", selected.board_id if selected else None)
        setattr(review, f"selected_{kind}_item_id", selected.item_id if selected else None)
    review.field_decisions_json = {label: saved.model_dump(mode="json") for label, saved in state.field_decisions.items()}
    review.source_dispositions_json = [entry.model_dump(mode="json") for entry in state.source_dispositions]
    review.decision_digest = decision_digest(state)
    review.source_fingerprint = source.identity.source_fingerprint
    review.source_identity_json = source.identity.model_dump(mode="json")
    review.desired_publication_identity = build_review_artifact_metadata(state, source)["publication_identity"]
    review.updated_at = _now()


def _queue(spec):
    spec.requested_generation += 1
    spec.scheduled_for = _now()
    # An old worker cannot promote its result or delay the newer generation.
    spec.lease_token = spec.lease_until = None


def _result(review, *, changed, event_id=None):
    return ReviewCommandResult(
        review_version=review.review_version, source_fingerprint=review.source_fingerprint,
        decision_digest=review.decision_digest,
        desired_publication_identity=review.desired_publication_identity,
        approval_id=str(review.current_approval_id) if review.current_approval_id else None,
        changed=changed, event_id=str(event_id) if event_id else None,
    )


def _event(db, review, *, command_type, before, after, source, actor=None,
           idempotency_key=None, request_digest=None, explanation=None, changed=True):
    event_id = uuid.uuid4()
    result = _result(review, changed=changed, event_id=event_id)
    db.add(PerformanceSpecificationReviewEvent(
        id=event_id, board_id=review.board_id, item_id=review.item_id,
        monday_account_id=review.monday_account_id,
        actor_app_user_id=actor.app_user_id if actor else "specification-review-worker",
        actor_monday_user_id=actor.monday_user_id if actor else None,
        command_type=command_type, prior_review_version=before.review_version,
        resulting_review_version=after.review_version,
        original_decisions_json=before.model_dump(mode="json"),
        replacement_decisions_json=after.model_dump(mode="json"),
        source_snapshot_json=source.model_dump(mode="json"),
        base_source_fingerprint=source.identity.source_fingerprint,
        explanation=explanation, idempotency_key=idempotency_key,
        request_digest=request_digest or review_digest({
            "command": command_type, "source": source.identity.model_dump(mode="json"),
            "before": before.model_dump(mode="json"), "after": after.model_dump(mode="json"),
            "explanation": explanation,
        }), result_json=result.model_dump(mode="json"),
        created_at=_now(),
    ))
    return result


def _invalidate(db, review, state, source, *, actor=None, reason):
    if review.current_approval_id is not None:
        previous_id = str(review.current_approval_id)
        review.current_approval_id = None
        _event(db, review, command_type="approval_invalidated", before=state, after=state,
               source=source, actor=actor, explanation=f"{reason}; prior approval {previous_id}")


def synchronize_review_source(db, *, board_id, item_id, account_id, source: ReviewSourceSnapshot,
                              expected_source_fingerprint: str | None = None):
    """Persist an actually refreshed recommendation identity, retaining decisions.

    A changed source invalidates completion and fences existing workers atomically.
    Replacing sources requires the identity observed before the remote refresh,
    so a slow read cannot overwrite a newer refresh. The caller must not use
    this function to relabel old extraction as current.
    """
    source = ReviewSourceSnapshot.model_validate(source)
    _clean_session(db)
    with db.begin():
        spec, review = _locked_rows(db, board_id, item_id)
        created = review is None
        if created:
            # Validate the tenant format using the same identity contract as actors.
            from .specification_review_contract import MondayRecord
            MondayRecord(board_id=str(account_id), item_id=str(item_id))
            review = PerformanceSpecificationReview(
                board_id=str(board_id), item_id=str(item_id), monday_account_id=str(account_id),
                schema_version=1, review_version=0,
            )
            db.add(review)
            state = ReviewDecisionState()
        else:
            _tenant(review, account_id)
            if expected_source_fingerprint is not None and review.source_fingerprint != expected_source_fingerprint:
                raise ReviewConflict("source_changed", "A newer source refresh was already saved")
            state = review_state(review)
            desired = build_review_artifact_metadata(state, source)["publication_identity"]
            if review.source_identity_json == source.identity.model_dump(mode="json") and review.desired_publication_identity == desired:
                return _result(review, changed=False)
            if expected_source_fingerprint is None:
                raise ReviewConflict("source_changed", "Replacing recommendations requires their previous source fingerprint")
            if review.source_fingerprint == source.identity.source_fingerprint:
                raise ReviewConflict("source_changed", "Changed recommendations require a distinct source fingerprint")
            _invalidate(db, review, state, source, reason="Relevant sources changed")
        _write_state(review, state, source)
        db.flush()
        _queue(spec)
        return _event(db, review, command_type="source_initialized" if created else "source_changed",
                      before=state, after=state, source=source)


initialize_review = synchronize_review_source


def observe_publication_source(db, review, *, expected_state: ReviewDecisionState,
                               source: ReviewSourceSnapshot):
    """Observe sources within the publisher's fenced, short transaction.

    The caller locks the specification row and then the review row after all
    remote reads. Unlike an explicit refresh command this does not queue another
    generation: the current worker is already producing the replacement PDF.
    Never create a tenant-bound review here without verified account identity.
    """
    if review is None:
        if expected_state != ReviewDecisionState():
            raise ReviewConflict("stale_review", "Review decisions changed during source resolution")
        return
    state = review_state(review)
    if state != expected_state:
        raise ReviewConflict("stale_review", "Review decisions changed during source resolution")
    desired = build_review_artifact_metadata(state, source)["publication_identity"]
    if (review.source_identity_json == source.identity.model_dump(mode="json")
            and review.desired_publication_identity == desired):
        return
    _invalidate(db, review, state, source, reason="Relevant publication sources changed")
    _write_state(review, state, source)
    _event(db, review, command_type="source_changed", before=state, after=state,
           source=source, explanation="Publication refreshed the relevant source identity")


def invalidate_publication_approval(db, review, *, reason):
    """Clear completion when live input no longer matches available extraction.

    The publisher already holds the specification and review locks. Audit against
    the previously approved snapshot; unavailable extraction must never be
    relabelled as a newly analysed source or discard the saved decisions.
    """
    if review is None or review.current_approval_id is None:
        return
    approval = db.get(PerformanceSpecificationApproval, review.current_approval_id)
    artifact = db.get(PerformanceSpecificationArtifact, (
        review.board_id, review.item_id, approval.artifact_revision,
    ))
    snapshot = (artifact.source_json or {}).get("reviewSource") if artifact is not None else None
    if snapshot is None:
        # Approvals created through the persistence contract always have an
        # immutable event, including older review artifacts without reviewSource.
        event = db.query(PerformanceSpecificationReviewEvent).filter_by(
            board_id=review.board_id, item_id=review.item_id,
            base_source_fingerprint=approval.source_fingerprint,
        ).order_by(PerformanceSpecificationReviewEvent.created_at.desc()).first()
        snapshot = event.source_snapshot_json if event is not None else None
    source = ReviewSourceSnapshot.model_validate(snapshot)
    _invalidate(db, review, review_state(review), source, reason=reason)
    review.updated_at = _now()


def _retry(db, review, actor, key, request_digest):
    event = db.query(PerformanceSpecificationReviewEvent).filter_by(
        board_id=review.board_id, item_id=review.item_id,
        monday_account_id=actor.monday_account_id, actor_monday_user_id=actor.monday_user_id,
        idempotency_key=key,
    ).one_or_none()
    if event is None:
        return None
    if event.request_digest != request_digest or event.actor_app_user_id != actor.app_user_id:
        raise ReviewConflict("idempotency_conflict", "The idempotency key was already used for a different command")
    return ReviewCommandResult.model_validate(event.result_json)


def _expected(review, command, source):
    if review.review_version != command.expected_review_version:
        raise ReviewConflict("stale_review", "Another reviewer saved newer decisions")
    if (review.source_fingerprint != command.expected_source_fingerprint
            or review.source_identity_json != source.identity.model_dump(mode="json")
            or build_review_artifact_metadata(review_state(review), source)["publication_identity"] != review.desired_publication_identity):
        raise ReviewConflict("source_changed", "Recommendations changed; reload before saving")


def _command_digest(kind, command):
    body = command.model_dump(mode="json")
    if isinstance(command, SaveReviewDecisions):
        # Selection omission is a patch; explicit null is a deliberate clear.
        body["selections"] = command.selections.model_dump(mode="json", exclude_unset=True) if command.selections is not None else None
        body["source_dispositions"] = sorted(body["source_dispositions"], key=lambda entry: (entry["source_id"], entry["issue_code"]))
    return review_digest({"command": kind, "body": body})


def _selections(state, patch, source):
    if patch is None:
        return state.selections
    values = state.selections.model_dump()
    values.update(patch.model_dump(exclude_unset=True))
    try:
        selected = ReviewSelections.model_validate(values)
    except ValueError as exc:
        raise ReviewConflict("candidates_ambiguous", "A drawing selection requires its project") from exc
    # Existing choices survive source outages; new choices must be scoped candidates.
    for kind in ("project", "drawing"):
        chosen = getattr(selected, f"selected_{kind}")
        if chosen is None or chosen == getattr(state.selections, f"selected_{kind}"):
            continue
        if source.candidates.status != "available":
            raise ReviewConflict("source_unavailable", "Candidate lookup is not available")
        candidates = source.candidates.projects if kind == "project" else source.candidates.drawings
        matches = [candidate for candidate in candidates if candidate.record == chosen]
        if not matches:
            raise ReviewConflict("candidates_ambiguous", "Selection is not a current candidate")
        if kind == "drawing" and not any(candidate.project == selected.selected_project and candidate.lifecycle == "active" for candidate in matches):
            raise ReviewConflict("candidates_ambiguous", "Drawing must be active and belong to the selected project")
    if selected.selected_drawing is not None:
        # A project change cannot silently keep a drawing from the previous roof.
        if selected.selected_project != state.selections.selected_project and not any(
            candidate.record == selected.selected_drawing and candidate.project == selected.selected_project
            and candidate.lifecycle == "active" for candidate in source.candidates.drawings
        ):
            raise ReviewConflict("candidates_ambiguous", "Re-select a drawing for the new project")
    return selected


def save_review_decisions(db, *, board_id, item_id, actor: ReviewActor,
                          command: SaveReviewDecisions, source: ReviewSourceSnapshot):
    actor = ReviewActor.model_validate(actor)
    command = SaveReviewDecisions.model_validate(command)
    source = ReviewSourceSnapshot.model_validate(source)
    request_digest = _command_digest("save_decisions", command)
    _clean_session(db)
    with db.begin():
        spec, review = _locked_rows(db, board_id, item_id)
        _tenant(review, actor.monday_account_id)
        retry = _retry(db, review, actor, command.idempotency_key, request_digest)
        if retry is not None:
            return retry
        _expected(review, command, source)
        before = review_state(review)
        after = before.model_copy(deep=True)
        after.selections = _selections(before, command.selections, source)
        audit = DecisionAudit(actor=actor, saved_at=_now(), reviewed_source=source.identity,
                              explanation=command.explanation)
        for label, operation in command.field_operations.items():
            if operation.operation == "restore_recommendation":
                after.field_decisions.pop(label, None)
                continue
            if operation.operation == "accept_recommendation":
                proposal = source.fields[label].proposal
                if not source_context_matches(after, source):
                    raise ReviewConflict("source_changed", "Refresh recommendations for the selected request type, project and drawing before accepting them")
                if proposal.proposal_id != operation.proposal_id or proposal.source == "unavailable":
                    raise ReviewConflict("source_changed", "The selected recommendation is no longer available")
                decision = AcceptedRecommendation(operation="accept_recommendation", snapshot=proposal.model_copy(deep=True))
            elif operation.operation == "specify_value":
                from .performance_specification_contract import reviewer_cad_mapping
                decision = SpecifiedValue(operation="specify_value", display_value=operation.display_value,
                                          cad_mapping=reviewer_cad_mapping(label, operation.display_value))
            else:
                decision = SpecifiedBlank(operation="specify_blank")
            after.field_decisions[label] = SavedFieldDecision(decision=decision, audit=audit)
        dispositions = {(entry.source_id, entry.issue_code): entry for entry in after.source_dispositions}
        issues = {(entry.source_id, entry.code) for entry in source.source_issues}
        if len({(entry.source_id, entry.issue_code) for entry in command.source_dispositions}) != len(command.source_dispositions):
            raise ReviewConflict("source_changed", "Each source issue must have a single disposition")
        for disposition in command.source_dispositions:
            key = (disposition.source_id, disposition.issue_code)
            if key not in issues:
                raise ReviewConflict("source_changed", "Source issue is not part of the current recommendations")
            dispositions[key] = SourceDisposition(**disposition.model_dump(), audit=audit)
        after.source_dispositions = [dispositions[key] for key in sorted(dispositions)]
        changed = decision_digest(before) != decision_digest(after)
        if changed:
            after.review_version += 1
            _invalidate(db, review, before, source, actor=actor, reason="Saved decisions changed")
            _write_state(review, after, source)
            _queue(spec)
        else:
            # A receipt describes the persisted result, including the original
            # attribution; a no-op does not pretend a fresh audit was applied.
            after = before
        return _event(db, review, command_type="save_decisions", before=before, after=after,
                      source=source, actor=actor, idempotency_key=command.idempotency_key,
                      request_digest=request_digest, explanation=command.explanation, changed=changed)


def _approval_ready(state, source, metadata):
    selected = state.selections
    if selected.selected_request_type is None or not source_context_matches(state, source):
        raise ReviewConflict("source_changed", "Refresh recommendations for the selected request type, project and drawing")
    if selected.selected_request_type == "amendment" and (
        selected.selected_project is None or selected.selected_drawing is None
    ):
        raise ReviewConflict("candidates_ambiguous", "Choose the amendment project and drawing")
    if selected.selected_drawing is not None and not any(
        candidate.record == selected.selected_drawing and candidate.project == selected.selected_project
        and candidate.lifecycle == "active" for candidate in source.candidates.drawings
    ):
        raise ReviewConflict("source_changed", "The selected drawing is no longer an active candidate")
    for label, field in metadata["field_provenance"].items():
        if field["requires_reconfirmation"]:
            raise ReviewConflict("source_changed", "Reconfirm decisions against current sources")
        if field["source"] == "unavailable" or (source.fields[label].issues and label not in state.field_decisions):
            raise ReviewConflict("source_unavailable", "Resolve outstanding field recommendations")
        if field["source"] not in {"reviewer_specified", "unassigned", "default_blank"} and label not in state.field_decisions:
            raise ReviewConflict("stale_review", "Accept or specify each populated recommendation")
    addressed = {(entry.source_id, entry.issue_code) for entry in state.source_dispositions
                 if entry.audit.reviewed_source == source.identity}
    if any((issue.source_id, issue.code) not in addressed for issue in source.source_issues):
        raise ReviewConflict("source_unavailable", "Review outstanding source issues")


def approve_review(db, *, board_id, item_id, actor: ReviewActor,
                   command: ApproveReview, source: ReviewSourceSnapshot):
    """Record immutable approval of an exact current, uploaded review artifact.

    This DB gate complements (and cannot replace) the review API's live access,
    Landing Zone, feature flag and artifact-integrity checks performed beforehand.
    """
    actor = ReviewActor.model_validate(actor)
    command = ApproveReview.model_validate(command)
    source = ReviewSourceSnapshot.model_validate(source)
    request_digest = _command_digest("approve", command)
    _clean_session(db)
    with db.begin():
        spec, review = _locked_rows(db, board_id, item_id)
        _tenant(review, actor.monday_account_id)
        retry = _retry(db, review, actor, command.idempotency_key, request_digest)
        if retry is not None:
            return retry
        _expected(review, command, source)
        if (review.decision_digest != command.expected_decision_digest
                or review.desired_publication_identity != command.expected_publication_identity):
            raise ReviewConflict("stale_review", "The PDF does not reflect current decisions")
        if (spec.current_revision != command.artifact_revision
                or spec.requested_generation != spec.completed_generation or spec.lease_token is not None):
            raise ReviewConflict("stale_review", "A newer PDF is pending or the selected PDF is superseded")
        artifact = db.get(PerformanceSpecificationArtifact, (str(board_id), str(item_id), command.artifact_revision))
        state = review_state(review)
        metadata = build_review_artifact_metadata(state, source)
        artifact_source = artifact.source_json if artifact is not None else None
        if (artifact is None or artifact.status != "published" or not artifact.monday_asset_id
                or not artifact.content_sha256 or artifact.size_bytes <= 0
                or not isinstance(artifact_source, dict) or artifact_source.get("review") != metadata):
            raise ReviewConflict("stale_review", "An exact published review artifact is required")
        _approval_ready(state, source, metadata)
        existing = db.get(PerformanceSpecificationApproval, review.current_approval_id) if review.current_approval_id else None
        changed = existing is None or (
            existing.artifact_revision != command.artifact_revision
            or existing.review_version != state.review_version
            or existing.source_fingerprint != source.identity.source_fingerprint
            or existing.extraction_identity != source.identity.extraction_identity
            or existing.decision_digest != review.decision_digest
        )
        if changed:
            _invalidate(db, review, state, source, actor=actor, reason="A different exact artifact is being approved")
            approval_id = uuid.uuid4()
            db.add(PerformanceSpecificationApproval(
                id=approval_id, board_id=str(board_id), item_id=str(item_id),
                monday_account_id=actor.monday_account_id, artifact_revision=command.artifact_revision,
                review_version=state.review_version, source_fingerprint=source.identity.source_fingerprint,
                extraction_identity=source.identity.extraction_identity, decision_digest=review.decision_digest,
                selected_project_board_id=review.selected_project_board_id,
                selected_project_item_id=review.selected_project_item_id,
                selected_drawing_board_id=review.selected_drawing_board_id,
                selected_drawing_item_id=review.selected_drawing_item_id,
                actor_app_user_id=actor.app_user_id, actor_monday_user_id=actor.monday_user_id,
                approved_at=_now(),
            ))
            db.flush()
            review.current_approval_id = approval_id
            review.updated_at = _now()
        return _event(db, review, command_type="approve", before=state, after=state,
                      source=source, actor=actor, idempotency_key=command.idempotency_key,
                      request_digest=request_digest, explanation=command.explanation, changed=changed)
