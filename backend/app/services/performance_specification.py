"""Durable, fenced refresh and publication of the active project's CAD PDF.

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
    DesignProcessingItem, Task, PerformanceSpecification,
    PerformanceSpecificationArtifact, PerformanceSpecificationLink,
)
from .design_processing_artifacts import SupabaseDesignArtifactStorage
from .design_processing_queue import execution_allowed_for_item
from .legacy_enquiry.llm import LegacyGeminiClient
from .performance_specification_contract import (
    CADTranslation, SPECIFICATION_VERSION, digest, translate_specification,
)
from .performance_specification_pdf import render_performance_specification_pdf
from .performance_specification_sources import (
    MondaySpecificationGateway, SpecificationIneligible, SpecificationSourceError,
    active_specification_group_ids, resolve_specification_sources,
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


def enqueue_specification(db, board_id, item_id, *, group_id=None, immediate=False):
    if not settings.performance_specification_enabled:
        return None
    board_id, item_id = str(board_id), str(item_id)
    if board_id != str(settings.design_processing_board_id):
        return None
    if group_id is not None and group_id not in active_specification_group_ids():
        cancel_specification(db, board_id, item_id)
        return None
    db.flush()
    if db.query(DesignProcessingItem.id).filter_by(board_id=board_id, item_id=item_id).first() is None:
        return None  # Extraction will enqueue when its first durable result exists.
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    db.execute(insert(PerformanceSpecification).values(
        board_id=board_id, item_id=item_id, requested_generation=0, completed_generation=0,
        attempt_count=0,
    ).on_conflict_do_nothing(index_elements=["board_id", "item_id"]))
    record = _record(db, board_id, item_id).one()
    record.requested_generation += 1
    due = utc_now() + timedelta(seconds=0 if immediate else settings.auto_sync_debounce_seconds)
    # Immediate work must not be postponed by a later duplicate notification.
    record.scheduled_for = min(_aware(record.scheduled_for), due) if record.scheduled_for else due
    record.attempt_count = 0
    return record


def cancel_specification(db, board_id, item_id):
    if not settings.performance_specification_enabled:
        return
    record = _record(db, str(board_id), str(item_id)).one_or_none()
    if record is not None:
        record.completed_generation = record.requested_generation
        record.scheduled_for = None
        record.lease_token = record.lease_until = None


def enqueue_specification_dependents(db, board_id, item_id):
    if not settings.performance_specification_enabled:
        return 0
    dependents = db.query(PerformanceSpecificationLink).filter_by(
        linked_board_id=str(board_id), linked_item_id=str(item_id),
    ).order_by(PerformanceSpecificationLink.board_id, PerformanceSpecificationLink.item_id).all()
    for link in dependents:
        enqueue_specification(db, link.board_id, link.item_id)
    return len(dependents)


def discover_specifications(db, limit=100):
    """Bootstrap existing active projects, without forcing email re-extraction."""
    if not settings.performance_specification_enabled:
        return 0
    candidates = db.query(DesignProcessingItem.board_id, DesignProcessingItem.item_id).join(
        Task, and_(Task.board_id == DesignProcessingItem.board_id, Task.item_id == DesignProcessingItem.item_id),
    ).outerjoin(PerformanceSpecification, and_(
        PerformanceSpecification.board_id == DesignProcessingItem.board_id,
        PerformanceSpecification.item_id == DesignProcessingItem.item_id,
    )).filter(
        DesignProcessingItem.board_id == str(settings.design_processing_board_id),
        Task.auto_sync_state == "active", Task.source_group_id.in_(active_specification_group_ids()),
        PerformanceSpecification.item_id.is_(None),
    ).order_by(DesignProcessingItem.item_id).limit(limit).all()
    for board_id, item_id in candidates:
        enqueue_specification(db, board_id, item_id, immediate=True)
    db.commit()
    return len(candidates)


def claim_specification(db):
    now = utc_now()
    query = db.query(PerformanceSpecification).filter(
        PerformanceSpecification.scheduled_for <= now,
        or_(PerformanceSpecification.lease_until.is_(None), PerformanceSpecification.lease_until <= now),
    ).order_by(PerformanceSpecification.scheduled_for, PerformanceSpecification.item_id)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    record = query.first()
    if record is None:
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
    if not settings.performance_specification_enabled or not execution_allowed_for_item(
        mode=settings.design_processing_mode, execution_kind="analysis", item_id=item_id,
    ):
        db.rollback()
        raise SupersededSpecification("Specification processing disabled")
    record.lease_until = utc_now() + timedelta(seconds=LEASE_SECONDS)
    db.commit()
    return record


def _publication_allowed(item_id):
    return settings.performance_specification_enabled and execution_allowed_for_item(
        mode=settings.design_processing_mode, execution_kind="publication", item_id=item_id,
        allowlist_item_ids=settings.design_processing_allowlist_item_ids,
    )


def _source(db, claim, gateway):
    record = _check_claim(db, claim)
    item = db.query(DesignProcessingItem).filter_by(board_id=claim[0], item_id=claim[1]).populate_existing().one()
    previous = record.source_json
    db.commit()  # No database locks/transaction across Monday or model calls.
    source = resolve_specification_sources(gateway, item, previous)
    record = _check_claim(db, claim)
    return record, source


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


def _finish(db, claim, source, *, revision=None):
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
    if revision is not None:
        record.current_revision = revision
    _replace_dependencies(db, record, source)
    db.commit()


def execute_specification(db, claim, *, gateway, client, storage):
    board_id, item_id, token, generation = claim
    try:
        record, source = _source(db, claim, gateway)
        model_version = f"{SPECIFICATION_VERSION}:{client.model}:thinking-{client.thinking_level}"
        revision = digest({"source": source, "modelVersion": model_version})
        artifact = db.get(PerformanceSpecificationArtifact, (board_id, item_id, revision))
        if artifact is None:
            translation = translate_specification(source["raw"], client)
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
                filename=filename, storage_bucket=settings.design_processing_artifact_bucket,
                storage_object_key=object_key, content_sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content), status="rendered",
            )
            db.add(artifact)
            db.commit()
        else:
            CADTranslation.model_validate(artifact.translation_json)
            db.commit()
            content = storage.read_private(artifact.storage_bucket, artifact.storage_object_key)
            if hashlib.sha256(content).hexdigest() != artifact.content_sha256 or len(content) != artifact.size_bytes:
                raise SpecificationSourceError("Stored specification PDF failed its integrity check")

        if not _publication_allowed(item_id):
            _finish(db, claim, source)
            return "rendered"  # Existing shadow/allowlist policy also governs this publisher.

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
        _finish(db, claim, source, revision=revision)

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
            record.completed_generation = record.requested_generation
            record.scheduled_for = None
            record.last_error = None
            outcome = "excluded"
        elif isinstance(exc, SupersededSpecification):
            record.scheduled_for = utc_now() + timedelta(seconds=settings.auto_sync_debounce_seconds)
            outcome = "superseded"
        else:
            record.attempt_count += 1
            record.scheduled_for = utc_now() + timedelta(seconds=min(3600, 30 * 2 ** min(record.attempt_count, 7)))
            record.last_error = f"{type(exc).__name__}: {str(exc)[:400]}"
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
    if not settings.performance_specification_enabled or settings.design_processing_mode == "off":
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
    if not settings.performance_specification_enabled:
        logger.info("Performance specification publication is disabled")
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
