"""Authenticated API orchestration for Specification Review.

The HTTP boundary authenticates the user and checks current item access/scope.
This module performs source/storage reads without a database transaction, then
uses the persistence service's short, consistently ordered transactions. It never
starts chat ingestion or writes Monday business fields.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timezone
import hashlib
from types import SimpleNamespace
from typing import Annotated

from pydantic import Field

from ..models import (
    DesignProcessingItem, DesignProcessingJob, PerformanceSpecification, PerformanceSpecificationArtifact,
    PerformanceSpecificationApproval, PerformanceSpecificationReview,
    PerformanceSpecificationReviewEvent,
)
from . import specification_review as persistence
from .design_processing_artifacts import SupabaseDesignArtifactStorage
from .performance_specification_sources import (
    SpecificationIneligible, SpecificationReviewRequired, SpecificationSourceError,
    SpecificationSourceWaiting, resolve_specification_sources,
    preview_specification_sources,
)
from ..config import settings
from .specification_review_artifacts import build_review_artifact_metadata
from .specification_policy import specification_publication_allowed
from .specification_review_contract import (
    FIELD_NAMES, NonemptyText, ReviewDecisionState, ReviewModel, ReviewSourceSnapshot,
)


class RefreshReview(ReviewModel):
    expected_review_version: int = Field(ge=0, strict=True)
    expected_source_fingerprint: NonemptyText
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
    explanation: str | None = Field(default=None, max_length=4000)


def _copy_row(row):
    if row is None:
        return None
    return SimpleNamespace(**{column.key: deepcopy(getattr(row, column.key))
                              for column in row.__table__.columns})


def _analysis_progress(db, item):
    """Only current-identity stages/counts cross the lightweight polling API."""
    if (item is None or not item.latest_desired_input_revision
            or item.latest_desired_pipeline_version != settings.design_processing_pipeline_version):
        return None
    extraction = getattr(item, "extracted_parameters_json", None)
    extraction_input = (extraction or {}).get("inputRevision") if extraction is not None else getattr(item, "extraction_input", None)
    extraction_pipeline = (extraction or {}).get("pipelineVersion") if extraction is not None else getattr(item, "extraction_pipeline", None)
    current_extraction = (extraction_input == item.latest_desired_input_revision
                          and extraction_pipeline == item.latest_desired_pipeline_version)
    ready = (item.latest_analyzed_input_revision == item.latest_desired_input_revision
             and item.latest_analyzed_pipeline_version == item.latest_desired_pipeline_version)
    job = db.query(DesignProcessingJob.status, DesignProcessingJob.stage,
        DesignProcessingJob.created_at, DesignProcessingJob.started_at, DesignProcessingJob.updated_at,
        DesignProcessingJob.completed_at, DesignProcessingJob.heartbeat_at,
        DesignProcessingJob.result_json["progress"].label("progress"),
    ).filter_by(
        board_id=item.board_id, item_id=item.item_id,
        execution_input_revision=item.latest_desired_input_revision,
        execution_pipeline_version=item.latest_desired_pipeline_version,
    ).order_by(DesignProcessingJob.created_at.desc()).first()
    progress = job.progress if job else {}
    progress = progress if isinstance(progress, dict) else {}
    counts = progress.get("source_counts", {})
    counts = counts if isinstance(counts, dict) else {}
    result = {
        "status": job.status if job else "pending", "stage": job.stage if job else None,
        "ready": ready, "current_extraction": current_extraction,
        "extraction_identity": hashlib.sha256(
            f"{extraction_input}:{extraction_pipeline}".encode()).hexdigest() if current_extraction else None,
        "source_counts": {key: value for key, value in counts.items()
                          if key in {"total", "read", "skipped", "review", "failed"}
                          and type(value) is int and value >= 0},
    }
    phase = progress.get("phase")
    if phase in {"reading_sources", "extracting_fields", "matching", "rendering"}:
        result["phase"] = phase
    for key in ("created_at", "started_at", "updated_at", "completed_at", "heartbeat_at"):
        value = getattr(job, key, None) if job else None
        result[key] = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat() if value else None
    return result


def _context(db, board_id, item_id, account_id):
    """Detach a consistent read before doing any potentially slow remote work."""
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Review API operations require a clean Session")
    board_id, item_id, account_id = str(board_id), str(item_id), str(account_id)
    try:
        item = db.query(DesignProcessingItem).filter_by(
            board_id=board_id, item_id=item_id).populate_existing().one_or_none()
        spec = db.get(PerformanceSpecification, (board_id, item_id), populate_existing=True)
        review = db.get(PerformanceSpecificationReview, (board_id, item_id), populate_existing=True)
        if review is not None and review.monday_account_id != account_id:
            raise persistence.ReviewConflict("reviewer_forbidden", "The review belongs to another account")
        state = persistence.review_state(review) if review is not None else ReviewDecisionState()
        artifacts = db.query(PerformanceSpecificationArtifact).filter_by(
            board_id=board_id, item_id=item_id).all()
        approval = db.get(PerformanceSpecificationApproval, review.current_approval_id) \
            if review is not None and review.current_approval_id else None
        events = db.query(PerformanceSpecificationReviewEvent).filter_by(
            board_id=board_id, item_id=item_id).order_by(
                PerformanceSpecificationReviewEvent.created_at.desc(),
                PerformanceSpecificationReviewEvent.id.desc()).limit(25).all()
        retained = None
        if review is not None:
            matching = db.query(PerformanceSpecificationReviewEvent).filter_by(
                board_id=board_id, item_id=item_id,
                base_source_fingerprint=review.source_fingerprint).order_by(
                    PerformanceSpecificationReviewEvent.created_at.desc()).first()
            retained = deepcopy(matching.source_snapshot_json) if matching is not None else None
        if retained is None and spec is not None:
            parked = spec.review_json or {}
            current_source = parked.get("source") if parked.get("status") == "needs_review" else spec.source_json
            retained = deepcopy((current_source or {}).get("reviewSource"))
        return {
            "item": _copy_row(item), "spec": _copy_row(spec), "review": _copy_row(review),
            "state": state, "artifacts": [_copy_row(row) for row in artifacts],
            "approval": _copy_row(approval), "retained_source": retained,
            "history": [{"event_id": str(row.id), "command_type": row.command_type,
                         "review_version": row.resulting_review_version,
                         "actor_monday_user_id": row.actor_monday_user_id,
                         "created_at": row.created_at.isoformat(), "explanation": row.explanation}
                        for row in events],
            "analysis": _analysis_progress(db, item),
        }
    finally:
        db.rollback()


def _resolve(db, context, gateway):
    if context["item"] is None:
        raise persistence.ReviewConflict("source_waiting", "Waiting for item-scoped source analysis")
    try:
        if db.in_transaction():
            raise RuntimeError("Remote source reads must not hold a database transaction")
        resolved = resolve_specification_sources(gateway, context["item"],
            context["spec"].source_json if context["spec"] else None,
            review_state=context["state"])
    except SpecificationReviewRequired as exc:
        resolved = exc.source
    except SpecificationSourceWaiting as exc:
        raise persistence.ReviewConflict("source_waiting", "Waiting for current source analysis") from exc
    except SpecificationIneligible as exc:
        raise persistence.ReviewConflict("scope_changed", "The item is outside the editable specification groups") from exc
    except SpecificationSourceError as exc:
        raise persistence.ReviewConflict("source_unavailable", "Source recommendations are temporarily unavailable") from exc
    return ReviewSourceSnapshot.model_validate(resolved["reviewSource"])


def _synchronize(db, context, *, board_id, item_id, account_id, source):
    from .performance_specification import _ensure_record
    with db.begin():
        if context["spec"] is None:
            # Only a current extraction on an already registered item reaches
            # this boundary. Unknown historic items are never registered here.
            _ensure_record(db, str(board_id), str(item_id))
        spec, review = persistence._locked_rows(db, str(board_id), str(item_id))
        created = review is None
        if created:
            if context["review"] is not None:
                raise persistence.ReviewConflict("stale_review", "The review changed during source refresh")
            review = PerformanceSpecificationReview(board_id=str(board_id), item_id=str(item_id),
                monday_account_id=str(account_id), review_version=0, schema_version=1)
            db.add(review)
            state = ReviewDecisionState()
        else:
            persistence._tenant(review, account_id)
            state = persistence.review_state(review)
            if context["review"] is None or state != context["state"]:
                raise persistence.ReviewConflict("stale_review", "Decisions changed during source refresh")
            if review.source_fingerprint != context["review"].source_fingerprint:
                raise persistence.ReviewConflict("source_changed", "A newer source refresh was already saved")
            desired = build_review_artifact_metadata(state, source)["publication_identity"]
            if (review.source_identity_json == source.identity.model_dump(mode="json")
                    and review.desired_publication_identity == desired):
                if (spec.review_json or {}).get("reviewApiSourceState"):
                    spec.review_json = {key: value for key, value in spec.review_json.items()
                                        if key != "reviewApiSourceState"} or None
                return persistence._result(review, changed=False)
            if review.source_fingerprint == source.identity.source_fingerprint:
                raise persistence.ReviewConflict("source_changed", "Changed recommendations require a fresh source identity")
            persistence._invalidate(db, review, state, source, reason="Relevant sources changed")
        persistence._write_state(review, state, source)
        if (spec.review_json or {}).get("reviewApiSourceState"):
            spec.review_json = {key: value for key, value in spec.review_json.items()
                                if key != "reviewApiSourceState"} or None
        db.flush()
        persistence._queue(spec)
        return persistence._event(db, review,
            command_type="source_initialized" if created else "source_changed",
            before=state, after=state, source=source)


def _record_source_error(db, context, *, board_id, item_id, account_id, code):
    if context["spec"] is None:
        return
    with db.begin():
        spec, review = persistence._locked_rows(db, str(board_id), str(item_id))
        if review is not None:
            persistence._tenant(review, account_id)
            if context["review"] is None or review.source_fingerprint != context["review"].source_fingerprint:
                raise persistence.ReviewConflict("source_changed", "Sources changed during the live read")
            if persistence.review_state(review) != context["state"]:
                raise persistence.ReviewConflict("stale_review", "Decisions changed during the live read")
            if code == "source_waiting":
                persistence.invalidate_publication_approval(db, review,
                    reason="Current review input requires fresh extraction")
        # A failed live check must remain visible to lightweight polling. Worker
        # completion replaces its diagnostics, or the next verified read clears
        # just this API observation. Human decisions never live in this JSON.
        spec.review_json = {**(spec.review_json or {}), "reviewApiSourceState": code}


def _artifact_info(artifact, board_id, item_id):
    if artifact is None:
        return None
    return {"revision": artifact.revision, "filename": artifact.filename,
            "status": artifact.status, "published": artifact.status == "published",
            "publication_identity": (getattr(artifact, "publication_identity", None)
                or ((getattr(artifact, "source_json", None) or {}).get("review") or {}).get("publication_identity")),
            "size_bytes": artifact.size_bytes,
            "url": f"/api/specification-review/items/{board_id}/{item_id}/pdf/{artifact.revision}"}


def _status(*, spec, review, source_ready, exact, approval, editable, source_error=None,
            manual_publication=False, draft_ready=False):
    source_error = source_error or ((spec.review_json or {}).get("reviewApiSourceState") if spec else None)
    if not editable:
        return "read_only"
    if source_error:
        return source_error
    if (spec and (spec.review_json or {}).get("status") == "needs_review"
            and spec.scheduled_for is None and spec.lease_token is None):
        return "needs_review"
    if not source_ready or review is None:
        return "source_waiting"
    if spec and spec.last_error:
        return "publication_failed"
    if spec and (spec.requested_generation != spec.completed_generation or spec.lease_token):
        if manual_publication:
            return "publishing_pdf" if spec.requested_publication_revision else "preparing_draft"
        return "regenerating_pdf"
    if approval and exact:
        return "complete"
    if manual_publication and not exact:
        return "ready_to_publish" if draft_ready else "changes_recommended"
    return "awaiting_final_review" if exact else "needs_review"


def _payload(context, *, board_id, item_id, editable, source=None, source_error=None, provisional=False,
             manual_publication=False):
    spec, review, state = context["spec"], context["review"], context["state"]
    if provisional:
        state = ReviewDecisionState()
    if source is None and context["retained_source"] is not None:
        source = ReviewSourceSnapshot.model_validate(context["retained_source"])
    current = next((artifact for artifact in context["artifacts"]
                    if spec and artifact.revision == spec.current_revision), None)
    metadata = build_review_artifact_metadata(state, source) if source is not None else None
    matching = [artifact for artifact in context["artifacts"]
                if metadata and (artifact.source_json or {}).get("review") == metadata]
    # Prefer a matching private render for shadow inspection while retaining a
    # previously published PDF as a separate, explicitly labelled document.
    prepared = next((entry for entry in matching if spec and entry.revision == spec.prepared_revision), None)
    preview = prepared or (current if current in matching else next(iter(matching), current))
    exact = bool(current and current.status == "published" and metadata
                 and (not spec.prepared_revision or current.revision == spec.prepared_revision)
                 and (current.source_json or {}).get("review") == metadata)
    approval = context["approval"]
    status = _status(spec=spec, review=review, source_ready=source is not None, exact=exact,
                     approval=approval, editable=editable, source_error=source_error,
                     manual_publication=manual_publication, draft_ready=preview in matching)
    if provisional and editable:
        status = "analysis_pending"
    return {
        "board_id": str(board_id), "item_id": str(item_id), "status": status,
        "provisional": provisional, "analysis": context.get("analysis"),
        "extraction_identity": (context.get("analysis") or {}).get("extraction_identity"),
        "review_version": review.review_version if review else 0,
        "source_fingerprint": review.source_fingerprint if review and not provisional else None,
        "decision_digest": review.decision_digest if review else None,
        "desired_publication_identity": review.desired_publication_identity if review else None,
        "requested_publication_revision": spec.requested_publication_revision if spec else None,
        "publication_changes": _publication_changes(current, metadata) if not provisional else [],
        "review": state.model_dump(mode="json"),
        "source": source.model_dump(mode="json") if source else None,
        "fields": {label: {**source.fields[label].model_dump(mode="json"),
                           "decision": state.field_decisions[label].model_dump(mode="json")
                           if label in state.field_decisions else None,
                           "effective": metadata["field_provenance"][label]}
                   for label in FIELD_NAMES} if source else {},
        "candidates": source.candidates.model_dump(mode="json") if source else {"status": "pending", "projects": [], "drawings": [], "issues": []},
        "queue": {"requested_generation": spec.requested_generation,
                  "completed_generation": spec.completed_generation,
                  "running": spec.lease_token is not None, "failed": bool(spec.last_error)} if spec else None,
        "pdf": _artifact_info(preview, board_id, item_id) if not provisional else None,
        "published_pdf": _artifact_info(current, board_id, item_id),
        "approval": {"id": str(approval.id), "artifact_revision": approval.artifact_revision,
                     "review_version": approval.review_version,
                     "actor_monday_user_id": approval.actor_monday_user_id,
                     "approved_at": approval.approved_at.isoformat()} if approval and not provisional else None,
        "history": context["history"],
    }


def _publication_changes(current, metadata):
    if current is None or metadata is None:
        return []
    before = ((current.source_json or {}).get("review") or {}).get("field_provenance", {})
    changes = []
    for label, value in metadata["field_provenance"].items():
        prior = before.get(label, {})
        if any(prior.get(key) != value.get(key) for key in ("display_value", "source_label")):
            changes.append({"field": label, "published_value": prior.get("display_value"),
                            "proposed_value": value.get("display_value"),
                            "published_source": prior.get("source_label"), "proposed_source": value.get("source_label")})
    return changes


def get_workspace(db, *, board_id, item_id, account_id, gateway, editable, manual_publication=False):
    context = _context(db, board_id, item_id, account_id)
    source, source_error, provisional = None, None, False
    if editable:
        try:
            source = _resolve(db, context, gateway)
            _synchronize(db, context, board_id=board_id, item_id=item_id,
                         account_id=account_id, source=source)
            context = _context(db, board_id, item_id, account_id)
        except persistence.ReviewConflict as exc:
            if exc.code not in {"source_waiting", "source_unavailable"}:
                raise
            source_error = exc.code
            analysis = context.get("analysis") or {}
            if (exc.code == "source_waiting" and context["item"] is not None
                    and analysis.get("current_extraction") and not analysis.get("ready")):
                try:
                    preview = preview_specification_sources(gateway, context["item"])
                    source = ReviewSourceSnapshot.model_validate(preview["reviewSource"])
                    provisional = True
                except (SpecificationSourceError, ValueError):
                    pass  # Invalid/stale evidence never becomes an early preview.
            if provisional:
                return _payload(context, board_id=board_id, item_id=item_id, editable=editable,
                                source=source, provisional=True, manual_publication=manual_publication)
            if context["spec"]:
                _record_source_error(db, context, board_id=board_id, item_id=item_id,
                                     account_id=account_id, code=exc.code)
                context = _context(db, board_id, item_id, account_id)
    return _payload(context, board_id=board_id, item_id=item_id, editable=editable,
                    source=source, source_error=source_error, manual_publication=manual_publication)


def get_status(db, *, board_id, item_id, account_id, editable, manual_publication=False):
    """Poll identities without loading email snapshots, history or all artifacts."""
    board_id, item_id = str(board_id), str(item_id)
    try:
        spec_row = db.query(PerformanceSpecification.requested_generation,
            PerformanceSpecification.completed_generation, PerformanceSpecification.lease_token,
            PerformanceSpecification.scheduled_for,
            PerformanceSpecification.last_error, PerformanceSpecification.current_revision,
            PerformanceSpecification.requested_publication_revision,
            PerformanceSpecification.prepared_revision,
            PerformanceSpecification.review_json["reviewApiSourceState"].as_string().label("source_state"),
            PerformanceSpecification.review_json["status"].as_string().label("review_status")
        ).filter_by(board_id=board_id, item_id=item_id).one_or_none()
        spec = SimpleNamespace(**spec_row._asdict(),
            review_json={"reviewApiSourceState": spec_row.source_state, "status": spec_row.review_status}) if spec_row else None
        review = db.query(PerformanceSpecificationReview.review_version,
            PerformanceSpecificationReview.monday_account_id, PerformanceSpecificationReview.source_fingerprint,
            PerformanceSpecificationReview.decision_digest, PerformanceSpecificationReview.desired_publication_identity,
            PerformanceSpecificationReview.current_approval_id
        ).filter_by(board_id=board_id, item_id=item_id).one_or_none()
        if review is not None:
            persistence._tenant(review, account_id)
        artifact_query = db.query(PerformanceSpecificationArtifact.revision,
            PerformanceSpecificationArtifact.filename, PerformanceSpecificationArtifact.status,
            PerformanceSpecificationArtifact.size_bytes,
            PerformanceSpecificationArtifact.source_json["review"]["publication_identity"].as_string().label("publication_identity")
        ).filter_by(board_id=board_id, item_id=item_id)
        current = artifact_query.filter(PerformanceSpecificationArtifact.revision == spec.current_revision).one_or_none() \
            if spec and spec.current_revision else None
        exact = bool(current and current.status == "published" and review
                     and (not spec.prepared_revision or current.revision == spec.prepared_revision)
                     and current.publication_identity == review.desired_publication_identity)
        preview = current
        if not exact and review:
            matching = artifact_query.filter(
                PerformanceSpecificationArtifact.source_json["review"]["publication_identity"].as_string()
                == review.desired_publication_identity)
            preview = (matching.filter(PerformanceSpecificationArtifact.revision == spec.prepared_revision).first()
                       if spec and spec.prepared_revision else None) or matching.first() or current
        approval = db.query(PerformanceSpecificationApproval.id,
            PerformanceSpecificationApproval.artifact_revision, PerformanceSpecificationApproval.review_version,
            PerformanceSpecificationApproval.actor_monday_user_id, PerformanceSpecificationApproval.approved_at
        ).filter_by(id=review.current_approval_id).one_or_none() if review and review.current_approval_id else None
        item = db.query(DesignProcessingItem.board_id, DesignProcessingItem.item_id,
            DesignProcessingItem.latest_desired_input_revision, DesignProcessingItem.latest_desired_pipeline_version,
            DesignProcessingItem.latest_analyzed_input_revision, DesignProcessingItem.latest_analyzed_pipeline_version,
            DesignProcessingItem.extracted_parameters_json["inputRevision"].as_string().label("extraction_input"),
            DesignProcessingItem.extracted_parameters_json["pipelineVersion"].as_string().label("extraction_pipeline"),
        ).filter_by(board_id=board_id, item_id=item_id).one_or_none()
        analysis = _analysis_progress(db, item)
        provisional = bool(analysis and analysis["current_extraction"] and not analysis["ready"])
        source_error = "analysis_pending" if provisional else "source_waiting" if analysis and not analysis["ready"] else None
        return {"board_id": board_id, "item_id": item_id,
            "status": _status(spec=spec, review=review, source_ready=review is not None,
                              exact=exact, approval=approval, editable=editable, source_error=source_error,
                              manual_publication=manual_publication,
                              draft_ready=bool(preview and review and preview.publication_identity == review.desired_publication_identity)),
            "provisional": provisional, "analysis": analysis,
            "extraction_identity": analysis["extraction_identity"] if analysis else None,
            "review_version": review.review_version if review else 0,
            "source_fingerprint": review.source_fingerprint if review and not provisional else None,
            "decision_digest": review.decision_digest if review else None,
            "desired_publication_identity": review.desired_publication_identity if review else None,
            "requested_publication_revision": spec.requested_publication_revision if spec else None,
            "queue": {"requested_generation": spec.requested_generation,
                      "completed_generation": spec.completed_generation,
                      "running": spec.lease_token is not None, "failed": bool(spec.last_error)} if spec else None,
            "pdf": _artifact_info(preview, board_id, item_id) if not provisional else None,
            "published_pdf": _artifact_info(current, board_id, item_id),
            "approval": {"id": str(approval.id), "artifact_revision": approval.artifact_revision,
                         "review_version": approval.review_version,
                         "actor_monday_user_id": approval.actor_monday_user_id,
                         "approved_at": approval.approved_at.isoformat()} if approval and not provisional else None,
        }
    finally:
        db.rollback()


def _replay(db, *, board_id, item_id, actor, command, kind):
    try:
        review = db.get(PerformanceSpecificationReview, (str(board_id), str(item_id)), populate_existing=True)
        if review is None:
            return None
        persistence._tenant(review, actor.monday_account_id)
        return persistence._retry(db, review, actor, command.idempotency_key,
                                  persistence._command_digest(kind, command))
    finally:
        db.rollback()


def _mutation_source(db, *, board_id, item_id, actor, gateway):
    context = _context(db, board_id, item_id, actor.monday_account_id)
    try:
        source = _resolve(db, context, gateway)
    except persistence.ReviewConflict as exc:
        if exc.code in {"source_waiting", "source_unavailable"}:
            _record_source_error(db, context, board_id=board_id, item_id=item_id,
                account_id=actor.monday_account_id, code=exc.code)
        raise
    if context["review"] is None:
        raise persistence.ReviewConflict("source_waiting", "Open the workspace before saving decisions")
    _synchronize(db, context, board_id=board_id, item_id=item_id,
                 account_id=actor.monday_account_id, source=source)
    return source


def save_decisions(db, *, board_id, item_id, actor, command, gateway):
    command = persistence.SaveReviewDecisions.model_validate(command)
    retry = _replay(db, board_id=board_id, item_id=item_id, actor=actor, command=command, kind="save_decisions")
    if retry is not None:
        return retry
    try:
        source = _mutation_source(db, board_id=board_id, item_id=item_id, actor=actor, gateway=gateway)
    except persistence.ReviewConflict:
        # An identical in-flight request may have committed while our source
        # read was running. Its immutable receipt wins over the stale snapshot.
        retry = _replay(db, board_id=board_id, item_id=item_id,
                        actor=actor, command=command, kind="save_decisions")
        if retry is not None:
            return retry
        raise
    return persistence.save_review_decisions(db, board_id=board_id, item_id=item_id,
                                            actor=actor, command=command, source=source)


def refresh_recommendations(db, *, board_id, item_id, actor, command, gateway):
    command = RefreshReview.model_validate(command)
    retry = _replay(db, board_id=board_id, item_id=item_id, actor=actor, command=command, kind="refresh")
    if retry is not None:
        return retry
    context = _context(db, board_id, item_id, actor.monday_account_id)
    try:
        source = _resolve(db, context, gateway)
    except persistence.ReviewConflict as exc:
        if exc.code in {"source_waiting", "source_unavailable"}:
            _record_source_error(db, context, board_id=board_id, item_id=item_id,
                account_id=actor.monday_account_id, code=exc.code)
        raise
    request_digest = persistence._command_digest("refresh", command)
    with db.begin():
        spec, review = persistence._locked_rows(db, str(board_id), str(item_id))
        persistence._tenant(review, actor.monday_account_id)
        retry = persistence._retry(db, review, actor, command.idempotency_key, request_digest)
        if retry is not None:
            return retry
        if review.review_version != command.expected_review_version:
            raise persistence.ReviewConflict("stale_review", "Another reviewer saved newer decisions")
        if review.source_fingerprint != command.expected_source_fingerprint:
            raise persistence.ReviewConflict("source_changed", "Recommendations already changed")
        state = persistence.review_state(review)
        if state != context["state"]:
            raise persistence.ReviewConflict("stale_review", "Decisions changed during source refresh")
        metadata = build_review_artifact_metadata(state, source)
        changed = (review.source_identity_json != source.identity.model_dump(mode="json")
                   or review.desired_publication_identity != metadata["publication_identity"])
        if changed:
            if review.source_fingerprint == source.identity.source_fingerprint:
                raise persistence.ReviewConflict("source_changed", "Changed recommendations require a fresh source identity")
            persistence._invalidate(db, review, state, source, actor=actor, reason="Reviewer refreshed sources")
            persistence._write_state(review, state, source)
            persistence._queue(spec)
        if (spec.review_json or {}).get("reviewApiSourceState"):
            spec.review_json = {key: value for key, value in spec.review_json.items()
                                if key != "reviewApiSourceState"} or None
        return persistence._event(db, review, command_type="refresh", before=state, after=state,
            source=source, actor=actor, idempotency_key=command.idempotency_key,
            request_digest=request_digest, explanation=command.explanation, changed=changed)


def _stored_bytes(db, artifact, storage):
    if db.in_transaction():
        raise RuntimeError("Remote PDF reads must not hold a database transaction")
    try:
        content = (storage or SupabaseDesignArtifactStorage()).read_private(
            artifact.storage_bucket, artifact.storage_object_key)
    except Exception as exc:
        raise persistence.ReviewConflict("publication_failed", "The retained PDF is temporarily unavailable") from exc
    if (not isinstance(content, bytes) or len(content) != artifact.size_bytes
            or hashlib.sha256(content).hexdigest() != artifact.content_sha256):
        raise persistence.ReviewConflict("publication_failed", "The retained PDF failed its integrity check")
    return content


def read_pdf(db, *, board_id, item_id, account_id, revision, storage=None):
    context = _context(db, board_id, item_id, account_id)
    artifact = next((entry for entry in context["artifacts"] if entry.revision == revision), None)
    if artifact is None:
        raise persistence.ReviewConflict("source_unavailable", "The requested PDF revision does not exist")
    return artifact.filename, _stored_bytes(db, artifact, storage)


def publish_specification(db, *, board_id, item_id, actor, command, gateway, storage=None):
    command = persistence.PublishReview.model_validate(command)
    if not specification_publication_allowed(item_id):
        raise persistence.ReviewConflict("reviewer_forbidden", "Specification publication is currently disabled")
    retry = _replay(db, board_id=board_id, item_id=item_id, actor=actor, command=command, kind="publish")
    if retry is not None:
        return retry
    source = _mutation_source(db, board_id=board_id, item_id=item_id, actor=actor, gateway=gateway)
    context = _context(db, board_id, item_id, actor.monday_account_id)
    artifact = next((entry for entry in context["artifacts"] if entry.revision == command.artifact_revision), None)
    if artifact is None or (artifact.source_json or {}).get("review") != build_review_artifact_metadata(context["state"], source):
        raise persistence.ReviewConflict("source_changed", "Prepare and inspect the current draft before publishing")
    _stored_bytes(db, artifact, storage)
    source = _mutation_source(db, board_id=board_id, item_id=item_id, actor=actor, gateway=gateway)
    if not specification_publication_allowed(item_id):
        raise persistence.ReviewConflict("reviewer_forbidden", "Specification publication is currently disabled")
    return persistence.request_publication(db, board_id=board_id, item_id=item_id,
                                         actor=actor, command=command, source=source)


def approve_specification(db, *, board_id, item_id, actor, command, gateway, storage=None):
    command = persistence.ApproveReview.model_validate(command)
    if not specification_publication_allowed(item_id):
        raise persistence.ReviewConflict("reviewer_forbidden", "Specification completion is currently disabled for this item")
    retry = _replay(db, board_id=board_id, item_id=item_id, actor=actor, command=command, kind="approve")
    if retry is not None:
        return retry
    source = _mutation_source(db, board_id=board_id, item_id=item_id, actor=actor, gateway=gateway)
    context = _context(db, board_id, item_id, actor.monday_account_id)
    artifact = next((entry for entry in context["artifacts"] if entry.revision == command.artifact_revision), None)
    if (artifact is None or artifact.status != "published" or not artifact.monday_asset_id
            or context["spec"].current_revision != command.artifact_revision):
        raise persistence.ReviewConflict("stale_review", "The selected PDF is not the current published artifact")
    _stored_bytes(db, artifact, storage)
    try:
        assets = gateway.inspect_files(str(item_id))
    except Exception as exc:
        raise persistence.ReviewConflict("publication_failed", "The published Monday PDF could not be verified") from exc
    matches = [asset for asset in assets if asset.filename == artifact.filename]
    if (len(matches) != 1 or str(matches[0].asset_id) != str(artifact.monday_asset_id)
            or matches[0].size_bytes != artifact.size_bytes):
        raise persistence.ReviewConflict("publication_failed", "The exact published Monday PDF is missing or ambiguous")
    # Re-read sources after storage/asset verification. Monday and Postgres cannot
    # share a transaction; the final DB command fences concurrent saves/workers.
    source = _mutation_source(db, board_id=board_id, item_id=item_id, actor=actor, gateway=gateway)
    # Live remote verification can outlast a rollout change or publisher pause.
    # Recheck immediately before the short approval transaction; saved decisions
    # remain available while publication/completion is disabled.
    if not specification_publication_allowed(item_id):
        raise persistence.ReviewConflict("reviewer_forbidden", "Specification completion is currently disabled for this item")
    return persistence.approve_review(db, board_id=board_id, item_id=item_id,
                                     actor=actor, command=command, source=source)
