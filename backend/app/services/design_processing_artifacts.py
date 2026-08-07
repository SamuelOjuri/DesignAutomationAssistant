from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from typing import Iterable, Mapping, Optional, Protocol
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..monday_client import MondayFileColumnAsset
from ..models import DesignProcessingArtifact, DesignProcessingItem
from .design_processing_state import (
    AI_DATA_COLUMN_ID,
    MATCH_REPORT_COLUMN_ID,
    ProcessingIdentity,
)
from .legacy_enquiry.formatting import build_ai_data_csv_bytes
from .match_report import MatchReport, render_match_report_pdf
from .storage_ingest import upload_with_retry


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactPublicationError(RuntimeError):
    pass


class AmbiguousArtifactAdoptionError(ArtifactPublicationError):
    pass


class DesignArtifactStorage(Protocol):
    def write_private(
        self,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> None: ...

    def read_private(self, bucket: str, object_key: str) -> bytes: ...


class SupabaseDesignArtifactStorage:
    def write_private(
        self,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> None:
        upload_with_retry(bucket, object_key, content, content_type)

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def read_private(self, bucket: str, object_key: str) -> bytes:
        from ..supabase_client import supabase

        content = supabase.storage.from_(bucket).download(object_key)
        return bytes(content)


@dataclass(frozen=True, slots=True)
class RenderedArtifact:
    artifact_kind: str
    column_id: str
    filename: str
    content_type: str
    content: bytes


def pipeline_digest(pipeline_version: str) -> str:
    return hashlib.sha256(pipeline_version.encode("utf-8")).hexdigest()


def deterministic_artifact_filenames(
    item_id: str,
    identity: ProcessingIdentity,
) -> tuple[str, str]:
    input_label = identity.input_revision[:12]
    pipeline_label = pipeline_digest(identity.pipeline_version)[:12]
    return (
        f"AI_Data_{item_id}_{input_label}_{pipeline_label}.csv",
        f"Matched_Projects_{item_id}_{input_label}_{pipeline_label}.pdf",
    )


def build_artifact_object_key(
    *,
    board_id: str,
    item_id: str,
    identity: ProcessingIdentity,
    filename: str,
) -> str:
    return (
        f"design-processing/{board_id}/{item_id}/{identity.input_revision}/"
        f"{pipeline_digest(identity.pipeline_version)}/{filename}"
    )


def render_analysis_artifacts(
    *,
    item_id: str,
    identity: ProcessingIdentity,
    parameters: Mapping[str, str],
    sources: Mapping[str, str],
    report: MatchReport,
) -> tuple[RenderedArtifact, RenderedArtifact]:
    csv_filename, pdf_filename = deterministic_artifact_filenames(item_id, identity)
    return (
        RenderedArtifact(
            artifact_kind="ai_data",
            column_id=AI_DATA_COLUMN_ID,
            filename=csv_filename,
            content_type="text/csv; charset=utf-8",
            content=build_ai_data_csv_bytes(parameters, sources),
        ),
        RenderedArtifact(
            artifact_kind="match_report",
            column_id=MATCH_REPORT_COLUMN_ID,
            filename=pdf_filename,
            content_type="application/pdf",
            content=render_match_report_pdf(report),
        ),
    )


def _artifact_query(
    db: Session,
    item: DesignProcessingItem,
    identity: ProcessingIdentity,
    *,
    artifact_kind: str,
    column_id: str,
):
    return db.query(DesignProcessingArtifact).filter(
        DesignProcessingArtifact.board_id == item.board_id,
        DesignProcessingArtifact.item_id == item.item_id,
        DesignProcessingArtifact.column_id == column_id,
        DesignProcessingArtifact.artifact_kind == artifact_kind,
        DesignProcessingArtifact.input_revision == identity.input_revision,
        DesignProcessingArtifact.pipeline_version == identity.pipeline_version,
    )


def verify_stored_artifact(
    artifact: DesignProcessingArtifact,
    *,
    storage: DesignArtifactStorage,
) -> bytes:
    content = storage.read_private(
        artifact.storage_bucket,
        artifact.storage_object_key,
    )
    digest = hashlib.sha256(content).hexdigest()
    if digest != artifact.content_sha256 or len(content) != artifact.size_bytes:
        raise ArtifactIntegrityError(
            f"stored {artifact.artifact_kind} artifact does not match its database hash"
        )
    return content


def select_adoptable_monday_asset(
    artifact: DesignProcessingArtifact,
    assets: Iterable[MondayFileColumnAsset],
) -> Optional[MondayFileColumnAsset]:
    if artifact.status not in {"uploading", "failed", "published"}:
        raise ArtifactPublicationError(
            f"cannot adopt {artifact.artifact_kind} from {artifact.status!r} status"
        )
    exact_name = [
        asset
        for asset in assets
        if asset.filename == artifact.deterministic_filename
    ]
    matching = [
        asset
        for asset in exact_name
        if asset.size_bytes is None or asset.size_bytes == artifact.size_bytes
    ]
    if len(matching) > 1:
        raise AmbiguousArtifactAdoptionError(
            f"multiple Monday assets match {artifact.deterministic_filename!r}"
        )
    if exact_name and not matching:
        raise ArtifactPublicationError(
            f"Monday asset {artifact.deterministic_filename!r} has the wrong size"
        )
    return matching[0] if matching else None


def prepare_artifact_upload(
    artifact: DesignProcessingArtifact,
    *,
    now: datetime,
) -> bool:
    if artifact.status == "published" and artifact.monday_asset_id is not None:
        return False
    if artifact.status not in {"rendered", "uploading", "failed", "published"}:
        raise ArtifactPublicationError(
            f"cannot publish {artifact.artifact_kind} from {artifact.status!r} status"
        )
    artifact.status = "uploading"
    artifact.last_error = None
    artifact.updated_at = now
    return True


def record_artifact_published(
    artifact: DesignProcessingArtifact,
    *,
    monday_asset_id: str,
    now: datetime,
) -> None:
    normalized_asset_id = str(monday_asset_id).strip()
    if not normalized_asset_id.isdecimal() or int(normalized_asset_id) <= 0:
        raise ArtifactPublicationError("Monday asset ID must be a positive decimal ID")
    if artifact.status not in {"uploading", "failed", "published"}:
        raise ArtifactPublicationError(
            f"cannot record {artifact.artifact_kind} from {artifact.status!r} status"
        )
    artifact.monday_asset_id = normalized_asset_id
    artifact.status = "published"
    artifact.last_error = None
    artifact.updated_at = now


def record_artifact_publication_error(
    artifact: DesignProcessingArtifact,
    *,
    error: str,
    now: datetime,
) -> None:
    if artifact.status == "published" and artifact.monday_asset_id is not None:
        return
    artifact.status = "failed"
    artifact.last_error = error[:2000]
    artifact.updated_at = now


def mark_prior_artifacts_delete_pending(
    db: Session,
    item: DesignProcessingItem,
    current_identity: ProcessingIdentity,
    *,
    now: datetime,
) -> tuple[DesignProcessingArtifact, ...]:
    prior = (
        db.query(DesignProcessingArtifact)
        .filter(
            DesignProcessingArtifact.board_id == item.board_id,
            DesignProcessingArtifact.item_id == item.item_id,
            DesignProcessingArtifact.status == "published",
            DesignProcessingArtifact.monday_asset_id.isnot(None),
            ~(
                (DesignProcessingArtifact.input_revision == current_identity.input_revision)
                & (
                    DesignProcessingArtifact.pipeline_version
                    == current_identity.pipeline_version
                )
            ),
        )
        .all()
    )
    for artifact in prior:
        artifact.status = "delete_pending"
        artifact.last_error = None
        artifact.updated_at = now
    return tuple(prior)


def record_artifact_deleted(
    artifact: DesignProcessingArtifact,
    *,
    now: datetime,
) -> None:
    if artifact.status != "delete_pending" or artifact.monday_asset_id is None:
        raise ArtifactPublicationError("artifact is not pending Monday deletion")
    artifact.status = "deleted"
    artifact.last_error = None
    artifact.updated_at = now


def record_artifact_cleanup_error(
    artifact: DesignProcessingArtifact,
    *,
    error: str,
    now: datetime,
) -> None:
    if artifact.status != "delete_pending":
        raise ArtifactPublicationError("artifact is not pending Monday deletion")
    artifact.last_error = error[:2000]
    artifact.updated_at = now


def find_verified_rendered_artifacts(
    db: Session,
    item: DesignProcessingItem,
    identity: ProcessingIdentity,
    *,
    storage: DesignArtifactStorage,
) -> tuple[DesignProcessingArtifact, ...]:
    artifacts = (
        db.query(DesignProcessingArtifact)
        .filter(
            DesignProcessingArtifact.board_id == item.board_id,
            DesignProcessingArtifact.item_id == item.item_id,
            DesignProcessingArtifact.input_revision == identity.input_revision,
            DesignProcessingArtifact.pipeline_version == identity.pipeline_version,
            DesignProcessingArtifact.status.in_(("rendered", "published")),
        )
        .all()
    )
    required = {
        ("ai_data", AI_DATA_COLUMN_ID),
        ("match_report", MATCH_REPORT_COLUMN_ID),
    }
    matching = {
        (artifact.artifact_kind, artifact.column_id): artifact
        for artifact in artifacts
        if (artifact.artifact_kind, artifact.column_id) in required
    }
    if set(matching) != required:
        return ()
    ordered = tuple(matching[key] for key in sorted(required))
    for artifact in ordered:
        verify_stored_artifact(artifact, storage=storage)
    return ordered


def persist_rendered_artifact(
    db: Session,
    item: DesignProcessingItem,
    identity: ProcessingIdentity,
    rendered: RenderedArtifact,
    *,
    bucket: str,
    storage: DesignArtifactStorage,
    now: datetime,
) -> DesignProcessingArtifact:
    existing = _artifact_query(
        db,
        item,
        identity,
        artifact_kind=rendered.artifact_kind,
        column_id=rendered.column_id,
    ).one_or_none()
    digest = hashlib.sha256(rendered.content).hexdigest()
    object_key = build_artifact_object_key(
        board_id=item.board_id,
        item_id=item.item_id,
        identity=identity,
        filename=rendered.filename,
    )
    if existing is not None and existing.status in {"rendered", "published"}:
        if (
            existing.deterministic_filename != rendered.filename
            or existing.storage_bucket != bucket
            or existing.storage_object_key != object_key
            or existing.content_sha256 != digest
            or existing.size_bytes != len(rendered.content)
        ):
            raise ArtifactIntegrityError(
                f"existing {rendered.artifact_kind} metadata differs for the same identity"
            )
        verify_stored_artifact(existing, storage=storage)
        return existing

    storage.write_private(
        bucket,
        object_key,
        rendered.content,
        rendered.content_type,
    )
    if existing is not None:
        existing.deterministic_filename = rendered.filename
        existing.storage_bucket = bucket
        existing.storage_object_key = object_key
        existing.content_sha256 = digest
        existing.size_bytes = len(rendered.content)
        existing.status = "rendered"
        existing.last_error = None
        existing.updated_at = now
        return existing

    artifact = DesignProcessingArtifact(
        id=uuid.uuid4(),
        board_id=item.board_id,
        item_id=item.item_id,
        column_id=rendered.column_id,
        artifact_kind=rendered.artifact_kind,
        input_revision=identity.input_revision,
        pipeline_version=identity.pipeline_version,
        deterministic_filename=rendered.filename,
        storage_bucket=bucket,
        storage_object_key=object_key,
        content_sha256=digest,
        size_bytes=len(rendered.content),
        monday_asset_id=None,
        status="rendered",
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(artifact)
            db.flush([artifact])
        return artifact
    except IntegrityError:
        raced = _artifact_query(
            db,
            item,
            identity,
            artifact_kind=rendered.artifact_kind,
            column_id=rendered.column_id,
        ).one()
        if raced.content_sha256 != digest or raced.size_bytes != len(rendered.content):
            raise ArtifactIntegrityError(
                f"concurrent {rendered.artifact_kind} artifact differs for the same identity"
            )
        verify_stored_artifact(raced, storage=storage)
        return raced
