"""Durable, fenced refresh and publication of Landing Zone specification PDFs.

Run separately from ingestion with:
    python -m backend.app.services.performance_specification --once
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import time
import uuid

from sqlalchemy import and_, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..config import settings
from ..db import SessionLocal
from ..models import (
    DesignProcessingItem, DesignProcessingReconciliationCheck, PerformanceSpecification,
    PerformanceSpecificationArtifact, PerformanceSpecificationLink,
    PerformanceSpecificationReview,
)
from .design_processing_artifacts import SupabaseDesignArtifactStorage
from .legacy_enquiry.llm import LegacyGeminiClient
from .performance_specification_contract import (
    SPECIFICATION_VERSION, CADTranslation, SpecificationPublication, digest,
    prepare_specification_publication, specification_translation_inputs,
)
from .specification_review import (
    ReviewConflict, invalidate_publication_approval, observe_publication_source, review_state,
)
from .specification_review_contract import SOURCE_POLICY_VERSION, ReviewDecisionState, ReviewSourceSnapshot
from .specification_references import REFERENCE_PARSER_VERSION
from .specification_policy import (
    SPECIFICATION_SCOPE_POLICY_VERSION, specification_scope_exclusion,
    specification_processing_enabled, specification_processing_allowed,
    specification_publication_allowed,
)
from .auto_sync_policy import RETIRED_SOURCE_OUTCOME
from .specification_review_artifacts import build_review_artifact_metadata
from .performance_specification_pdf import render_performance_specification_pdf
from .performance_specification_sources import (
    MondaySpecificationGateway, SpecificationIneligible, SpecificationSourceError,
    SpecificationReviewRequired, SpecificationSourceWaiting,
    resolve_specification_sources,
)


logger = logging.getLogger(__name__)
LEASE_SECONDS = 900


def utc_now():
    return datetime.now(timezone.utc)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _record(db, board_id, item_id):
    query = db.query(PerformanceSpecification).filter_by(
        board_id=board_id, item_id=item_id,
    ).populate_existing()
    return query.with_for_update() if db.get_bind().dialect.name == "postgresql" else query


def _ensure_record(db, board_id, item_id):
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    db.execute(insert(PerformanceSpecification).values(
        board_id=board_id, item_id=item_id, requested_generation=0, completed_generation=0,
        attempt_count=0,
    ).on_conflict_do_nothing(index_elements=["board_id", "item_id"]))
    return _record(db, board_id, item_id).one()


def _obsolete_policy(record):
    source = record.source_json or {}
    return (
        source.get("scopePolicy") != SPECIFICATION_SCOPE_POLICY_VERSION
        or source.get("sourcePolicyVersion") != SOURCE_POLICY_VERSION
        or source.get("referenceParserVersion") != REFERENCE_PARSER_VERSION
        or source.get("extractionPipeline") != settings.design_processing_pipeline_version
        or source.get("publicationVersion") != SPECIFICATION_VERSION
    )


def specification_needs_reconciliation(record):
    """Recover missing/cancelled or obsolete settled work without waking retries."""
    if record is None:
        return True
    if (record.lease_token is not None and record.lease_until is not None
            and _aware(record.lease_until) > utc_now()):
        return False
    if record.scheduled_for is None:
        return True  # Also repair legacy rows with an outstanding, unscheduled generation.
    if record.requested_generation > record.completed_generation:
        return False
    return not record.attempt_count and _obsolete_policy(record)


def enqueue_specification(db, board_id, item_id, *, group_id=None, immediate=False, reconcile=False):
    board_id, item_id = str(board_id), str(item_id)
    if board_id != str(settings.design_processing_board_id):
        return None
    if group_id is not None:
        reason = specification_scope_exclusion(board_id=board_id, group_id=group_id, item_state="active")
        if reason is not None:
            cancel_specification(db, board_id, item_id, reason=reason)
            return None
    if not specification_processing_allowed(item_id):
        return None
    db.flush()
    if db.query(DesignProcessingItem.id).filter_by(board_id=board_id, item_id=item_id).first() is None:
        return None  # Registered admission/extraction, never a Task, is the prerequisite.
    record = _ensure_record(db, board_id, item_id)
    if reconcile and not specification_needs_reconciliation(record):
        return record
    record.requested_generation += 1
    due = utc_now() + timedelta(seconds=0 if immediate else settings.auto_sync_debounce_seconds)
    # Immediate work must not be postponed by a later duplicate notification.
    if record.scheduled_for:
        choose = max if record.attempt_count and not immediate else min
        record.scheduled_for = choose(_aware(record.scheduled_for), due)
    else:
        record.scheduled_for = due
    # Failure history is cleared by a successful refresh, not by notifications.
    return record


def cancel_specification(db, board_id, item_id, *, reason="outside_landing_zone"):
    record = _record(db, str(board_id), str(item_id)).one_or_none()
    if record is not None:
        record.completed_generation = record.requested_generation
        record.scheduled_for = None
        record.lease_token = record.lease_until = None
        record.checked_at = utc_now()
        review = db.query(PerformanceSpecificationReview).filter_by(
            board_id=str(board_id), item_id=str(item_id),
        ).populate_existing().with_for_update().one_or_none()
        # A human move retains the exact PDF's completion/history. Live scope
        # denies further edits/publication; a relevant source change on re-entry
        # invalidates completion through observe_publication_source instead.
        record.review_json = {
            "status": "read_only", "reason": reason, "source": record.source_json,
            "left_before_completion": review is None or review.current_approval_id is None,
        }


def enqueue_specification_dependents(db, board_id, item_id, *, seen=None):
    if not specification_processing_enabled():
        return 0
    query = db.query(PerformanceSpecification).join(PerformanceSpecificationLink, and_(
        PerformanceSpecification.board_id == PerformanceSpecificationLink.board_id,
        PerformanceSpecification.item_id == PerformanceSpecificationLink.item_id,
    )).filter(
        PerformanceSpecification.board_id == str(settings.design_processing_board_id),
        PerformanceSpecificationLink.linked_board_id == str(board_id),
        PerformanceSpecificationLink.linked_item_id == str(item_id),
    ).filter(PerformanceSpecification.item_id.in_(settings.performance_specification_allowlist_item_ids)
             if settings.performance_specification_mode == "allowlist" else True
    ).order_by(PerformanceSpecification.board_id, PerformanceSpecification.item_id).populate_existing()
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(of=PerformanceSpecification)
    dependents = query.all()
    queued = 0
    for link in dependents:
        if (link.review_json or {}).get("status") == "read_only":
            # Keep historical dependencies, but scope observations/re-entry own
            # recovery. CRM edits must not continually refresh excluded items.
            continue
        identity = (link.board_id, link.item_id)
        if seen is not None:
            if identity in seen:
                continue
            seen.add(identity)
        enqueue_specification(db, link.board_id, link.item_id)
        queued += 1
    return queued


def discover_specifications(db, limit=100, *, gateway=None):
    """Fair, bounded recovery of registered work with a fresh live scope read.

    Design registration owns activation-bounded admission and explicit backfill.
    Its durable group observations exclude known historical non-Landing items.
    No Task/auto-sync state or document publication is required here.
    """
    if not specification_processing_enabled():
        return 0
    if limit < 1:
        raise ValueError("Specification discovery limit must be positive")
    now = utc_now()
    stale_before = now - timedelta(seconds=settings.performance_specification_recheck_seconds)
    check = DesignProcessingReconciliationCheck
    spec = PerformanceSpecification
    candidates = db.query(DesignProcessingItem.board_id, DesignProcessingItem.item_id).outerjoin(spec, and_(
        PerformanceSpecification.board_id == DesignProcessingItem.board_id,
        PerformanceSpecification.item_id == DesignProcessingItem.item_id,
    )).outerjoin(check, and_(
        check.board_id == DesignProcessingItem.board_id, check.item_id == DesignProcessingItem.item_id,
    )).filter(
        DesignProcessingItem.board_id == str(settings.design_processing_board_id),
        or_(check.last_outcome.is_(None), check.last_outcome != RETIRED_SOURCE_OUTCOME),
        or_(check.last_group_id.is_(None), check.last_group_id == str(settings.design_processing_landing_group_id)),
        # Scheduled rows already have periodic worker checks; cancelled/missing
        # rows need discovery. A newer Landing observation bypasses the delay.
        or_(spec.item_id.is_(None), and_(
            spec.scheduled_for.is_(None),
            or_(spec.checked_at.is_(None), spec.checked_at <= stale_before,
                check.last_checked_at > spec.checked_at),
        ), and_(
            spec.requested_generation == spec.completed_generation,
            spec.attempt_count == 0,
            or_(spec.source_json["scopePolicy"].as_string().is_(None),
                spec.source_json["scopePolicy"].as_string() != SPECIFICATION_SCOPE_POLICY_VERSION,
                spec.source_json["sourcePolicyVersion"].as_string().is_(None),
                spec.source_json["sourcePolicyVersion"].as_string() != SOURCE_POLICY_VERSION,
                spec.source_json["referenceParserVersion"].as_string().is_(None),
                spec.source_json["referenceParserVersion"].as_string() != REFERENCE_PARSER_VERSION,
                spec.source_json["extractionPipeline"].as_string().is_(None),
                spec.source_json["extractionPipeline"].as_string() != settings.design_processing_pipeline_version,
                spec.source_json["publicationVersion"].as_string().is_(None),
                spec.source_json["publicationVersion"].as_string() != SPECIFICATION_VERSION),
            spec.scheduled_for.is_not(None), spec.lease_token.is_(None),
            # A failed live observation must not make an obsolete settled row
            # consume the bounded discovery set on every worker poll. Its normal
            # scheduled claim remains available; newer observations can wake it.
            or_(spec.last_error.is_(None), ~spec.last_error.like("Discovery: %"),
                spec.checked_at.is_(None), spec.checked_at <= stale_before,
                check.last_checked_at > spec.checked_at),
        )),
    ).filter(DesignProcessingItem.item_id.in_(settings.performance_specification_allowlist_item_ids)
             if settings.performance_specification_mode == "allowlist" else True
    ).order_by(spec.checked_at.asc().nulls_first(), DesignProcessingItem.item_id).limit(limit).all()
    db.commit()  # Never hold a database transaction across the live read.
    if not candidates:
        return 0
    if gateway is None:
        from .auto_sync import get_monday_ingestion_access_token
        gateway = MondaySpecificationGateway(get_monday_ingestion_access_token())
    recovered = 0
    for board_id, item_id in candidates:
        if not specification_processing_allowed(item_id):
            continue
        try:
            target = gateway.fetch_target(item_id)
            if not specification_processing_allowed(item_id):
                continue
            reason = specification_scope_exclusion(target)
            if str(target.item_id) != item_id:
                raise SpecificationSourceError("Specification discovery returned a different item")
            record = _ensure_record(db, board_id, item_id)
            record.checked_at = now  # Discovery visit, including excluded/error rows, rotates fairly.
            if reason is not None:
                cancel_specification(db, board_id, item_id, reason=reason)
            elif specification_needs_reconciliation(record):
                enqueue_specification(db, board_id, item_id, group_id=target.group_id,
                                      immediate=True, reconcile=True)
                recovered += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            # Unavailable is not evidence of archive/delete. Retain the PDF and
            # decisions, record the failed observation, and let later checks retry.
            record = _ensure_record(db, board_id, item_id)
            record.checked_at = now
            record.last_error = f"Discovery: {type(exc).__name__}"
            db.commit()
            logger.warning("Specification discovery failed item=%s error_type=%s", item_id, type(exc).__name__)
    return recovered


def claim_specification(db):
    if not specification_processing_enabled():
        return None
    now = utc_now()
    query = db.query(PerformanceSpecification).filter(
        PerformanceSpecification.board_id == str(settings.design_processing_board_id),
        PerformanceSpecification.scheduled_for <= now,
        or_(PerformanceSpecification.lease_until.is_(None), PerformanceSpecification.lease_until <= now),
    ).filter(PerformanceSpecification.item_id.in_(settings.performance_specification_allowlist_item_ids)
             if settings.performance_specification_mode == "allowlist" else True
    ).order_by(PerformanceSpecification.scheduled_for, PerformanceSpecification.item_id)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    record = query.first()
    if record is None or not specification_processing_allowed(record.item_id):
        db.rollback()
        return None
    token = uuid.uuid4().hex
    record.lease_token, record.lease_until = token, now + timedelta(seconds=LEASE_SECONDS)
    claim = (record.board_id, record.item_id, token, record.requested_generation)
    db.commit()
    return claim


class SupersededSpecification(RuntimeError):
    pass


def _check_claim(db, claim):
    board_id, item_id, token, generation = claim
    record = _record(db, board_id, item_id).one_or_none()
    if (record is None or record.lease_token != token or record.requested_generation != generation
            or record.lease_until is None or _aware(record.lease_until) <= utc_now()):
        db.rollback()
        raise SupersededSpecification("Specification refresh lease/generation changed")
    if not specification_processing_allowed(item_id):
        db.rollback()
        raise SupersededSpecification("Specification processing disabled")
    record.lease_until = utc_now() + timedelta(seconds=LEASE_SECONDS)
    db.commit()
    return record


def _publication_allowed(item_id):
    return specification_publication_allowed(item_id)


def _source(db, claim, gateway):
    record = _check_claim(db, claim)
    item = db.query(DesignProcessingItem).filter_by(board_id=claim[0], item_id=claim[1]).populate_existing().one()
    previous = record.source_json
    review = db.query(PerformanceSpecificationReview).filter_by(
        board_id=claim[0], item_id=claim[1],
    ).populate_existing().one_or_none()
    state = review_state(review) if review is not None else ReviewDecisionState()
    db.commit()  # No database locks/transaction across Monday or model calls.
    source_failure = None
    try:
        source = resolve_specification_sources(gateway, item, previous, review_state=state)
    except SpecificationSourceWaiting:
        _check_claim(db, claim)
        record = _record(db, claim[0], claim[1]).one()
        if record.lease_token != claim[2] or record.requested_generation != claim[3]:
            db.rollback()
            raise SupersededSpecification("Specification changed while waiting for extraction")
        review = db.query(PerformanceSpecificationReview).filter_by(
            board_id=claim[0], item_id=claim[1],
        ).populate_existing().with_for_update().one_or_none()
        invalidate_publication_approval(db, review, reason="Current input requires fresh extraction")
        db.commit()
        raise
    except SpecificationReviewRequired as exc:
        if not exc.source.get("reviewSource"):
            raise
        # A changed source requiring correction still invalidates completion,
        # even though its provisional artifact cannot yet be rendered.
        source, source_failure = exc.source, exc
    snapshot = ReviewSourceSnapshot.model_validate(source["reviewSource"])
    source["review"] = build_review_artifact_metadata(state, snapshot)
    source["publicationVersion"] = SPECIFICATION_VERSION
    source["translationIdentity"] = digest(specification_translation_inputs(source))
    record = _check_claim(db, claim)
    # Lock in the same order as a reviewer save. A save after the remote reads
    # cannot be overwritten or published by an older worker.
    record = _record(db, claim[0], claim[1]).one()
    if record.lease_token != claim[2] or record.requested_generation != claim[3]:
        db.rollback()
        raise SupersededSpecification("Specification decisions changed during source resolution")
    review = db.query(PerformanceSpecificationReview).filter_by(
        board_id=claim[0], item_id=claim[1],
    ).populate_existing().with_for_update().one_or_none()
    try:
        observe_publication_source(db, review, expected_state=state, source=snapshot)
    except ReviewConflict as exc:
        db.rollback()
        raise SupersededSpecification(str(exc)) from exc
    db.commit()
    if source_failure is not None:
        raise source_failure
    return record, source


def _cached_translation(db, board_id, item_id, source, model_version):
    """Reuse validated CAD work across reviewer/provenance-only revisions."""
    artifact = db.query(PerformanceSpecificationArtifact).filter_by(
        board_id=board_id, item_id=item_id, model_version=model_version,
    ).filter(
        PerformanceSpecificationArtifact.source_json["translationIdentity"].as_string()
        == source["translationIdentity"],
    ).first()
    if artifact is not None:
        publication = SpecificationPublication.model_validate(artifact.translation_json)
        return CADTranslation(values=publication.values, warnings=publication.warnings)
    # A reviewer may replace one previously automatic field. Reuse the remaining
    # unchanged CAD results rather than translating them again. The publication
    # builder clears removed inputs and reapplies the current authoritative fields.
    record = db.get(PerformanceSpecification, (board_id, item_id))
    prior = (db.get(PerformanceSpecificationArtifact, (board_id, item_id, record.current_revision))
             if record and record.current_revision else None)
    if prior is not None and prior.model_version == model_version and (prior.source_json or {}).get("review"):
        current_inputs = specification_translation_inputs(source)
        prior_inputs = specification_translation_inputs(prior.source_json)
        required = [label for label, value in current_inputs["raw"].items()
                    if label != "Incoming Information" and value is not None and str(value).strip()]
        if all(current_inputs["raw"][label] == prior_inputs["raw"].get(label)
               and current_inputs["evidence"].get("fields", {}).get(label)
               == prior_inputs["evidence"].get("fields", {}).get(label) for label in required):
            publication = SpecificationPublication.model_validate(prior.translation_json)
            return CADTranslation(values=publication.values, warnings=publication.warnings)
    return None


def _replace_dependencies(db, record, source):
    db.query(PerformanceSpecificationLink).filter_by(board_id=record.board_id, item_id=record.item_id).delete()
    for board_id, item_id in source["dependencies"]:
        db.add(PerformanceSpecificationLink(
            board_id=record.board_id, item_id=record.item_id,
            linked_board_id=board_id, linked_item_id=item_id,
        ))


def _adoptable(artifact, assets):
    matches = [asset for asset in assets if asset.filename == artifact.filename]
    if len(matches) > 1:
        raise SpecificationSourceError("Several Monday files match this specification revision")
    if matches and matches[0].size_bytes not in {None, artifact.size_bytes}:
        raise SpecificationSourceError("Existing specification file has a different size")
    return matches[0] if matches else None


def _finish(db, claim, source, publication, *, revision=None):
    _check_claim(db, claim)
    if revision is not None and not _publication_allowed(claim[1]):
        raise SupersededSpecification("Specification publication disabled before completion")
    record = _record(db, claim[0], claim[1]).one()
    if record.lease_token != claim[2] or record.requested_generation != claim[3]:
        db.rollback()
        raise SupersededSpecification("Specification changed before completion")
    record.source_json = source
    record.checked_at = utc_now()
    record.completed_generation = claim[3]
    record.scheduled_for = utc_now() + timedelta(seconds=settings.performance_specification_recheck_seconds)
    record.lease_token = record.lease_until = None
    record.last_error, record.attempt_count = None, 0
    record.review_json = {
        "status": "published_with_review" if revision is not None else "rendered_with_review",
        "source": source, "issues": publication.field_reviews,
        "sourceReviews": publication.source_reviews,
        "mappingIssues": publication.mapping_issues,
        "translation": publication.model_dump(by_alias=True),
    } if (publication.field_reviews or publication.request_notes or publication.source_reviews
          or publication.mapping_issues) else None
    if revision is not None:
        record.current_revision = revision
    _replace_dependencies(db, record, source)
    db.commit()


def execute_specification(db, claim, *, gateway, client, storage):
    board_id, item_id, token, generation = claim
    source = None
    try:
        record, source = _source(db, claim, gateway)
        model_version = f"{SPECIFICATION_VERSION}:{client.model}:thinking-{client.thinking_level}"
        revision = digest({"review": source["review"], "modelVersion": model_version})
        artifact = db.get(PerformanceSpecificationArtifact, (board_id, item_id, revision))
        if artifact is None:
            cached = _cached_translation(db, board_id, item_id, source, model_version)
            db.commit()
            translation = prepare_specification_publication(source, client, cached_translation=cached)
            _check_claim(db, claim)
            # Re-read source and group after translation; a late model response
            # cannot publish facts that were superseded during its request.
            _, current = _source(db, claim, gateway)
            if current != source:
                raise SupersededSpecification("Specification sources changed during translation")
            content = render_performance_specification_pdf(translation, item_id=item_id, revision=revision)
            filename = f"Project_Performance_Specification_Details_{item_id}_{revision}.pdf"
            object_key = f"performance-specifications/{board_id}/{item_id}/{revision}/{filename}"
            _check_claim(db, claim)
            storage.write_private(settings.design_processing_artifact_bucket, object_key, content, "application/pdf")
            _check_claim(db, claim)
            artifact = PerformanceSpecificationArtifact(
                board_id=board_id, item_id=item_id, revision=revision,
                translation_json=translation.model_dump(by_alias=True), model_version=model_version,
                source_json=source,
                filename=filename, storage_bucket=settings.design_processing_artifact_bucket,
                storage_object_key=object_key, content_sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content), status="rendered",
            )
            db.add(artifact)
            db.commit()
        else:
            translation = SpecificationPublication.model_validate(artifact.translation_json)
            db.commit()
            content = storage.read_private(artifact.storage_bucket, artifact.storage_object_key)
            if hashlib.sha256(content).hexdigest() != artifact.content_sha256 or len(content) != artifact.size_bytes:
                raise SpecificationSourceError("Stored specification PDF failed its integrity check")

        if not _publication_allowed(item_id):
            _finish(db, claim, source, translation)
            return "rendered"  # Specification rollout is independent of design exports.

        _, current = _source(db, claim, gateway)
        if current != source:
            raise SupersededSpecification("Specification sources changed before upload")
        assets = gateway.inspect_files(item_id)
        _check_claim(db, claim)
        candidate = _adoptable(artifact, assets)
        artifact.status = "uploading"
        db.commit()
        if not _publication_allowed(item_id):
            raise SupersededSpecification("Specification publication disabled")
        uploaded = candidate or gateway.upload(item_id, artifact.filename, content)
        # Persist the asset even when a notification arrived during upload. It
        # remains tracked for recovery/cleanup, never promoted as current below.
        artifact.monday_asset_id = uploaded.asset_id
        artifact.status = "uploaded"
        db.commit()
        _, current = _source(db, claim, gateway)
        if current != source or not _publication_allowed(item_id):
            raise SupersededSpecification("Specification changed during upload")
        artifact.status = "published"
        artifact.last_error = None
        db.commit()
        _finish(db, claim, source, translation, revision=revision)

        # Cleanup takes a separate fenced claim after current publication commits.
        _cleanup_specifications(db, board_id, item_id, revision, gateway, source)
        return "published" if candidate is None else "unchanged"
    except Exception as exc:
        db.rollback()
        record = _record(db, board_id, item_id).one_or_none()
        if record is None or record.lease_token != token:
            db.rollback()
            return "superseded"
        record.lease_token = record.lease_until = None
        if isinstance(exc, SpecificationIneligible):
            cancel_specification(db, board_id, item_id, reason=str(exc))
            record.last_error = None
            outcome = "excluded"
        elif isinstance(exc, SupersededSpecification):
            record.scheduled_for = utc_now() + timedelta(seconds=settings.auto_sync_debounce_seconds)
            outcome = "superseded"
        else:
            record.attempt_count += 1
            record.scheduled_for = utc_now() + timedelta(seconds=min(3600, 30 * 2 ** min(record.attempt_count, 7)))
            record.last_error = f"{type(exc).__name__}: {str(exc)[:400]}"
            if isinstance(exc, SpecificationReviewRequired):
                record.review_json = {"status": "conflict", "source": exc.source, "issues": exc.issues}
                _replace_dependencies(db, record, exc.source)
            elif isinstance(exc, SpecificationSourceError):
                record.review_json = {"status": "source_failure", "reason": str(exc)[:2000], "source": source}
            outcome = "failed"
            logger.warning("Specification refresh failed item=%s error_type=%s", item_id, type(exc).__name__)
        db.commit()
        return outcome


def _cleanup_specifications(db, board_id, item_id, revision, gateway, source):
    """Retain other AI Data files and remove only ledger-owned prior specifications."""
    record = _record(db, board_id, item_id).one()
    if (record.current_revision != revision or record.requested_generation != record.completed_generation
            or record.lease_token is not None or not _publication_allowed(item_id)):
        db.rollback()
        return
    token = uuid.uuid4().hex
    record.lease_token = token
    record.lease_until = utc_now() + timedelta(seconds=LEASE_SECONDS)
    claim = (board_id, item_id, token, record.requested_generation)
    db.commit()
    try:
        prior = db.query(PerformanceSpecificationArtifact).filter(
            PerformanceSpecificationArtifact.board_id == board_id,
            PerformanceSpecificationArtifact.item_id == item_id,
            PerformanceSpecificationArtifact.revision != revision,
            PerformanceSpecificationArtifact.status != "deleted",
        ).all()
        db.commit()
        for artifact in prior:
            _, current = _source(db, claim, gateway)
            if current != source or not _publication_allowed(item_id):
                raise SupersededSpecification("Specification changed before cleanup")
            assets = gateway.inspect_files(item_id)
            current_artifact = db.get(PerformanceSpecificationArtifact, (board_id, item_id, revision))
            if not any(asset.asset_id == current_artifact.monday_asset_id for asset in assets):
                raise SpecificationSourceError("Replacement specification is missing; cleanup stopped")
            owned = _adoptable(artifact, assets)
            _check_claim(db, claim)
            if not _publication_allowed(item_id):
                raise SupersededSpecification("Specification publication disabled before cleanup")
            if owned:
                gateway.delete(board_id, item_id, owned.asset_id)
            artifact.status, artifact.last_error = "deleted", None
            db.commit()
    except Exception as exc:
        db.rollback()
        record = _record(db, board_id, item_id).one()
        if record.lease_token == token:
            record.last_error = f"Specification cleanup: {type(exc).__name__}"
            record.scheduled_for = utc_now() + timedelta(seconds=60)
        db.commit()
    finally:
        record = _record(db, board_id, item_id).one()
        if record.lease_token == token:
            record.lease_token = record.lease_until = None
        db.commit()


def run_specifications_once(db, *, gateway=None, client=None, storage=None, limit=1):
    if not specification_processing_enabled():
        return []
    if limit < 1:
        raise ValueError("Specification claim limit must be positive")
    if gateway is None:
        from .auto_sync import get_monday_ingestion_access_token
        gateway = MondaySpecificationGateway(get_monday_ingestion_access_token())
    client = client or LegacyGeminiClient(settings.design_processing_extraction_model,
                                         thinking_level=settings.design_processing_thinking_level)
    storage = storage or SupabaseDesignArtifactStorage()
    outcomes = []
    for _ in range(limit):
        claim = claim_specification(db)
        if claim is None:
            break
        outcomes.append(execute_specification(db, claim, gateway=gateway, client=client, storage=storage))
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--item-id", help="Enqueue an existing design item for a fresh scope/source check")
    parser.add_argument("--poll-interval-seconds", type=float, default=5)
    args = parser.parse_args()
    if not specification_processing_enabled():
        logger.info("Performance specification processing is disabled")
        return 0
    while True:
        with SessionLocal() as db:
            if args.item_id:
                enqueue_specification(db, settings.design_processing_board_id, args.item_id, immediate=True)
                db.commit()
                args.item_id = None
            discover_specifications(db)
            outcomes = run_specifications_once(db)
            if outcomes:
                logger.info("Specification outcomes: %s", outcomes)
        if args.once:
            return 0
        time.sleep(max(args.poll_interval_seconds, 0.1))


if __name__ == "__main__":
    raise SystemExit(main())
