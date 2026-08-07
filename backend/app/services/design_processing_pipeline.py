from __future__ import annotations

from datetime import datetime
import logging
import os
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy.orm import Session

from ..models import (
    DesignProcessingArtifact,
    DesignProcessingItem,
    DesignProcessingJob,
)
from .auto_sync import utc_now
from .design_processing_artifacts import (
    DesignArtifactStorage,
    find_verified_rendered_artifacts,
    mark_prior_artifacts_delete_pending,
    persist_rendered_artifact,
    prepare_artifact_upload,
    record_artifact_cleanup_error,
    record_artifact_deleted,
    record_artifact_published,
    render_analysis_artifacts,
    select_adoptable_monday_asset,
    verify_stored_artifact,
)
from .design_processing_inputs import (
    DownloadedAssetLike,
    download_design_email_assets,
)
from .design_processing_observability import log_design_processing_event
from .design_processing_queue import (
    DesignProcessingMode,
    execution_allowed_for_item,
    queue_design_processing_snapshot,
)
from .design_processing_state import (
    AI_DATA_COLUMN_ID,
    MATCH_REPORT_COLUMN_ID,
    ProcessingIdentity,
    advance_stage,
    complete_analysis,
    complete_publication,
    desired_identity,
    execution_identity,
    published_identity,
)
from .design_processing_target import (
    DesignProcessingReadGateway,
    DesignProcessingTargetMismatch,
    assert_current_execution_target,
)
from .legacy_enquiry.analysis import (
    LegacyAnalysisClient,
    LegacyAnalysisResult,
    analyze_downloaded_email_assets,
)
from .legacy_enquiry.formatting import (
    format_date_for_monday,
    format_dropdown_for_monday,
    format_hour_for_monday,
)
from .legacy_enquiry.matching import build_matching_contract, match_projects
from .match_report import MatchReport


Clock = Callable[[], datetime]
AssetDownloader = Callable[[dict[str, Any], str], DownloadedAssetLike]
ExecutionPolicy = Callable[[str, str], bool]
logger = logging.getLogger(__name__)


def _execution_is_allowed(
    execution_kind: str,
    item_id: str,
    *,
    mode: DesignProcessingMode,
    allowlist_item_ids: Iterable[str],
    execution_policy: Optional[ExecutionPolicy],
) -> bool:
    if execution_policy is not None:
        return execution_policy(execution_kind, item_id)
    return execution_allowed_for_item(
        mode=mode,
        execution_kind=execution_kind,
        item_id=item_id,
        allowlist_item_ids=allowlist_item_ids,
    )


def _supports_row_locks(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def _lock_current_analysis(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    execution_policy: Optional[ExecutionPolicy] = None,
) -> tuple[DesignProcessingItem, DesignProcessingJob, ProcessingIdentity]:
    job_query = (
        db.query(DesignProcessingJob)
        .filter(DesignProcessingJob.id == job_id)
        .populate_existing()
    )
    if _supports_row_locks(db):
        job_query = job_query.with_for_update()
    job = job_query.one_or_none()
    if job is None:
        raise DesignProcessingTargetMismatch(
            "job_missing",
            "design-processing analysis job no longer exists",
        )

    item_query = (
        db.query(DesignProcessingItem)
        .filter(
            DesignProcessingItem.board_id == job.board_id,
            DesignProcessingItem.item_id == job.item_id,
        )
        .populate_existing()
    )
    if _supports_row_locks(db):
        item_query = item_query.with_for_update()
    item = item_query.one()
    if job.status != "running" or job.locked_by != worker_id:
        raise DesignProcessingTargetMismatch(
            "lease_lost",
            "design-processing analysis no longer owns its lease",
        )
    identity = execution_identity(job)
    if identity is None or job.execution_kind != "analysis":
        raise DesignProcessingTargetMismatch(
            "execution_changed",
            "job is not an assigned analysis execution",
        )
    if desired_identity(item) != identity:
        raise DesignProcessingTargetMismatch(
            "stored_identity_changed",
            "analysis execution was superseded before its checkpoint",
        )
    if execution_policy is not None and not execution_policy(
        "analysis",
        item.item_id,
    ):
        raise DesignProcessingTargetMismatch(
            "execution_disabled",
            "the current operational mode no longer permits analysis",
        )
    return item, job, identity


def _identity_payload(identity: ProcessingIdentity) -> dict[str, str]:
    return {
        "inputRevision": identity.input_revision,
        "pipelineVersion": identity.pipeline_version,
    }


def _has_identity(payload: Any, identity: ProcessingIdentity) -> bool:
    return (
        isinstance(payload, Mapping)
        and payload.get("inputRevision") == identity.input_revision
        and payload.get("pipelineVersion") == identity.pipeline_version
    )


def _extraction_payload(
    result: LegacyAnalysisResult,
    identity: ProcessingIdentity,
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        **_identity_payload(identity),
        "parameters": result.parameters,
        "sources": result.sources,
        "projectName": result.project_name,
        "emailContentHashes": [
            {
                "assetId": audit.asset_id,
                "filename": audit.filename,
                "contentSha256": audit.content_sha256,
                "sizeBytes": audit.size_bytes,
            }
            for audit in result.email_content_audit
        ],
        "extractedTextSha256": result.extracted_text_sha256,
    }


def _matching_payload(
    *,
    identity: ProcessingIdentity,
    match_contract: Mapping[str, Any],
    legacy_diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        **_identity_payload(identity),
        "result": dict(match_contract),
        "legacyDiagnostics": dict(legacy_diagnostics),
    }


def _update_job_progress(
    job: DesignProcessingJob,
    identity: ProcessingIdentity,
    *,
    completed_stage: str,
) -> None:
    job.result_json = {
        "schemaVersion": 1,
        **_identity_payload(identity),
        "completedStage": completed_stage,
    }


def _remove_downloaded_files(downloaded_assets: Iterable[Any]) -> None:
    for downloaded in downloaded_assets:
        try:
            os.unlink(downloaded.temp_path)
        except OSError:
            pass


def run_analysis_pipeline(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    access_token: str,
    gateway: DesignProcessingReadGateway,
    analysis_client: LegacyAnalysisClient,
    artifact_storage: DesignArtifactStorage,
    artifact_bucket: str,
    pipeline_version: str,
    expected_board_id: str,
    expected_group_id: str,
    mode: DesignProcessingMode,
    allowlist_item_ids: Iterable[str] = (),
    execution_policy: Optional[ExecutionPolicy] = None,
    downloader: Optional[AssetDownloader] = None,
    clock: Clock = utc_now,
) -> str:
    item, job, identity = _lock_current_analysis(
        db,
        job_id,
        worker_id=worker_id,
        execution_policy=execution_policy,
    )
    snapshot = assert_current_execution_target(
        item,
        job,
        gateway=gateway,
        pipeline_version=pipeline_version,
        expected_board_id=expected_board_id,
        expected_group_id=expected_group_id,
        worker_id=worker_id,
        execution_allowed=_execution_is_allowed(
            "analysis",
            item.item_id,
            mode=mode,
            allowlist_item_ids=allowlist_item_ids,
            execution_policy=execution_policy,
        ),
    )
    db.rollback()

    item, job, identity = _lock_current_analysis(
        db,
        job_id,
        worker_id=worker_id,
        execution_policy=execution_policy,
    )
    extraction = item.extracted_parameters_json
    if not _has_identity(extraction, identity):
        db.commit()
        downloaded_assets = download_design_email_assets(
            snapshot.email_assets,
            access_token,
            downloader=downloader,
        )
        try:
            extraction_result = analyze_downloaded_email_assets(
                downloaded_assets,
                client=analysis_client,
            )
        finally:
            _remove_downloaded_files(downloaded_assets)

        item, job, identity = _lock_current_analysis(
            db,
            job_id,
            worker_id=worker_id,
            execution_policy=execution_policy,
        )
        item.extracted_parameters_json = _extraction_payload(
            extraction_result,
            identity,
        )
        _update_job_progress(job, identity, completed_stage="extracting")
        advance_stage(job, "matching", worker_id=worker_id, now=clock())
        db.commit()
        extraction = item.extracted_parameters_json
    elif job.stage == "extracting":
        advance_stage(job, "matching", worker_id=worker_id, now=clock())
        db.commit()

    item, job, identity = _lock_current_analysis(
        db,
        job_id,
        worker_id=worker_id,
        execution_policy=execution_policy,
    )
    extraction = item.extracted_parameters_json
    if not _has_identity(extraction, identity):
        raise RuntimeError("current analysis extraction output is unavailable")
    matching = item.match_result_json
    if not _has_identity(matching, identity):
        project_name = str(extraction.get("projectName") or "")
        db.commit()
        if project_name:
            legacy_result = match_projects(project_name, gateway)
        else:
            legacy_result = {
                "exists": False,
                "type": "new",
                "matches": [],
                "best_match": None,
                "similarity_score": 0.0,
                "error": "",
            }
        match_contract = build_matching_contract(project_name, legacy_result)

        item, job, identity = _lock_current_analysis(
            db,
            job_id,
            worker_id=worker_id,
            execution_policy=execution_policy,
        )
        item.match_result_json = _matching_payload(
            identity=identity,
            match_contract=match_contract,
            legacy_diagnostics=legacy_result,
        )
        _update_job_progress(job, identity, completed_stage="matching")
        advance_stage(job, "rendering", worker_id=worker_id, now=clock())
        db.commit()
        matching = item.match_result_json
    elif job.stage in {"extracting", "matching"}:
        advance_stage(job, "rendering", worker_id=worker_id, now=clock())
        db.commit()

    item, job, identity = _lock_current_analysis(
        db,
        job_id,
        worker_id=worker_id,
        execution_policy=execution_policy,
    )
    extraction = item.extracted_parameters_json
    matching = item.match_result_json
    if not _has_identity(extraction, identity) or not _has_identity(matching, identity):
        raise RuntimeError("current analysis outputs are incomplete before rendering")

    existing_artifacts = find_verified_rendered_artifacts(
        db,
        item,
        identity,
        storage=artifact_storage,
    )
    if not existing_artifacts:
        parameters = dict(extraction.get("parameters") or {})
        sources = dict(extraction.get("sources") or {})
        match_contract = dict(matching.get("result") or {})
        report = MatchReport.from_contract(
            source_item_id=item.item_id,
            extracted_company=str(parameters.get("Company") or "Not found"),
            match_contract=match_contract,
        )
        rendered_artifacts = render_analysis_artifacts(
            item_id=item.item_id,
            identity=identity,
            parameters=parameters,
            sources=sources,
            report=report,
        )
        for rendered in rendered_artifacts:
            persist_rendered_artifact(
                db,
                item,
                identity,
                rendered,
                bucket=artifact_bucket,
                storage=artifact_storage,
                now=clock(),
            )
    db.commit()

    item, job, identity = _lock_current_analysis(
        db,
        job_id,
        worker_id=worker_id,
        execution_policy=execution_policy,
    )
    _update_job_progress(job, identity, completed_stage="rendering")
    completed_at = clock()
    complete_analysis(
        item,
        job,
        worker_id=worker_id,
        now=completed_at,
    )
    db.flush([item, job])

    publication_allowed = _execution_is_allowed(
        "publication",
        item.item_id,
        mode=mode,
        allowlist_item_ids=allowlist_item_ids,
        execution_policy=execution_policy,
    )
    if publication_allowed:
        queue_design_processing_snapshot(
            db,
            snapshot,
            trigger_type="analysis_completed",
            mode=mode,
            pipeline_version=pipeline_version,
            expected_board_id=expected_board_id,
            expected_group_id=expected_group_id,
            allowlist_item_ids=allowlist_item_ids,
            now=completed_at,
        )
    db.commit()
    return "analyzed"


def _lock_current_publication(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
) -> tuple[DesignProcessingItem, DesignProcessingJob, ProcessingIdentity]:
    job_query = (
        db.query(DesignProcessingJob)
        .filter(DesignProcessingJob.id == job_id)
        .populate_existing()
    )
    if _supports_row_locks(db):
        job_query = job_query.with_for_update()
    job = job_query.one_or_none()
    if job is None:
        raise DesignProcessingTargetMismatch(
            "job_missing",
            "design-processing publication job no longer exists",
        )
    item_query = (
        db.query(DesignProcessingItem)
        .filter(
            DesignProcessingItem.board_id == job.board_id,
            DesignProcessingItem.item_id == job.item_id,
        )
        .populate_existing()
    )
    if _supports_row_locks(db):
        item_query = item_query.with_for_update()
    item = item_query.one()
    identity = execution_identity(job)
    if (
        job.status != "running"
        or job.locked_by != worker_id
        or job.execution_kind != "publication"
        or identity is None
    ):
        raise DesignProcessingTargetMismatch(
            "lease_lost",
            "design-processing publication no longer owns its execution",
        )
    if desired_identity(item) != identity:
        raise DesignProcessingTargetMismatch(
            "stored_identity_changed",
            "publication execution was superseded before its checkpoint",
        )
    return item, job, identity


def _current_publication_artifacts(
    db: Session,
    item: DesignProcessingItem,
    identity: ProcessingIdentity,
) -> tuple[DesignProcessingArtifact, DesignProcessingArtifact]:
    artifacts = (
        db.query(DesignProcessingArtifact)
        .filter(
            DesignProcessingArtifact.board_id == item.board_id,
            DesignProcessingArtifact.item_id == item.item_id,
            DesignProcessingArtifact.input_revision == identity.input_revision,
            DesignProcessingArtifact.pipeline_version == identity.pipeline_version,
        )
        .all()
    )
    by_key = {
        (artifact.artifact_kind, artifact.column_id): artifact
        for artifact in artifacts
    }
    try:
        return (
            by_key[("ai_data", AI_DATA_COLUMN_ID)],
            by_key[("match_report", MATCH_REPORT_COLUMN_ID)],
        )
    except KeyError as exc:
        raise RuntimeError("current publication artifacts are incomplete") from exc


def _is_missing_extracted_value(value: Any) -> bool:
    return value is None or not str(value).strip() or str(value).strip().lower() in {
        "not found",
        "not available",
        "n/a",
    }


def build_design_owned_column_values(
    parameters: Mapping[str, Any],
    column_settings: Mapping[str, str],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    values: dict[str, Any] = {}
    warnings: list[str] = []

    raw_date = parameters.get("Date Received")
    formatted_date = (
        "" if _is_missing_extracted_value(raw_date) else format_date_for_monday(raw_date)
    )
    if formatted_date:
        values["date_mkpb23av"] = {"date": formatted_date}
    else:
        warnings.append("Date Received was not written because it is missing or invalid")

    raw_hour = parameters.get("Hour Received")
    formatted_hour = (
        None if _is_missing_extracted_value(raw_hour) else format_hour_for_monday(raw_hour)
    )
    if formatted_hour is not None:
        values["hour_mkpbb3j1"] = formatted_hour
    else:
        warnings.append("Hour Received was not written because it is missing or invalid")

    raw_zip = parameters.get("Zip Code")
    formatted_zip = (
        None
        if _is_missing_extracted_value(raw_zip)
        else format_dropdown_for_monday(
            raw_zip,
            column_settings.get("dropdown_mkpbafca", ""),
        )
    )
    if formatted_zip is not None:
        values["dropdown_mkpbafca"] = formatted_zip
    else:
        warnings.append("Zip Code was not written because it is missing or unmapped")
    return values, tuple(warnings)


def _append_item_warnings(
    item: DesignProcessingItem,
    warnings: Iterable[str],
) -> None:
    current = list(item.warnings_json or [])
    for warning in warnings:
        if warning not in current:
            current.append(warning)
    item.warnings_json = current


def _publish_one_artifact(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    gateway: DesignProcessingReadGateway,
    artifact_storage: DesignArtifactStorage,
    pipeline_version: str,
    expected_board_id: str,
    expected_group_id: str,
    mode: DesignProcessingMode,
    allowlist_item_ids: Iterable[str],
    execution_policy: Optional[ExecutionPolicy],
    artifact_kind: str,
    next_stage: Optional[str],
    clock: Clock,
) -> None:
    item, job, identity = _lock_current_publication(
        db,
        job_id,
        worker_id=worker_id,
    )
    if not _execution_is_allowed(
        "publication",
        item.item_id,
        mode=mode,
        allowlist_item_ids=allowlist_item_ids,
        execution_policy=execution_policy,
    ):
        raise DesignProcessingTargetMismatch(
            "execution_disabled",
            "the current operational mode no longer permits publication completion",
        )
    artifacts = _current_publication_artifacts(db, item, identity)
    artifact = next(
        value for value in artifacts if value.artifact_kind == artifact_kind
    )
    if artifact.status == "published" and artifact.monday_asset_id is not None:
        if next_stage is not None and job.stage != next_stage:
            advance_stage(job, next_stage, worker_id=worker_id, now=clock())
            db.commit()
        else:
            db.rollback()
        return
    prepare_artifact_upload(artifact, now=clock())
    db.commit()

    content = verify_stored_artifact(artifact, storage=artifact_storage)
    assets_by_column = gateway.inspect_file_columns(item.item_id)
    candidate = select_adoptable_monday_asset(
        artifact,
        assets_by_column.get(artifact.column_id, ()),
    )

    item, job, identity = _lock_current_publication(
        db,
        job_id,
        worker_id=worker_id,
    )
    artifact = next(
        value
        for value in _current_publication_artifacts(db, item, identity)
        if value.artifact_kind == artifact_kind
    )
    assert_current_execution_target(
        item,
        job,
        gateway=gateway,
        pipeline_version=pipeline_version,
        expected_board_id=expected_board_id,
        expected_group_id=expected_group_id,
        worker_id=worker_id,
        execution_allowed=_execution_is_allowed(
            "publication",
            item.item_id,
            mode=mode,
            allowlist_item_ids=allowlist_item_ids,
            execution_policy=execution_policy,
        ),
    )
    if candidate is None:
        content_type = (
            "text/csv; charset=utf-8"
            if artifact.artifact_kind == "ai_data"
            else "application/pdf"
        )
        candidate = gateway.upload_design_file(
            item.item_id,
            artifact.column_id,
            artifact.deterministic_filename,
            content,
            content_type,
        )
        if (
            candidate.filename != artifact.deterministic_filename
            or (
                candidate.size_bytes is not None
                and candidate.size_bytes != artifact.size_bytes
            )
        ):
            raise RuntimeError("Monday uploaded artifact metadata does not match")
    published_at = clock()
    record_artifact_published(
        artifact,
        monday_asset_id=candidate.asset_id,
        now=published_at,
    )
    _update_job_progress(job, identity, completed_stage=job.stage or "publication")
    if next_stage is not None:
        advance_stage(job, next_stage, worker_id=worker_id, now=published_at)
    db.commit()


def cleanup_delete_pending_artifacts(
    db: Session,
    *,
    gateway: DesignProcessingReadGateway,
    item_id: Optional[str] = None,
    limit: Optional[int] = None,
    cleanup_policy: Optional[Callable[[str], bool]] = None,
    clock: Clock = utc_now,
) -> tuple[int, int]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    query = db.query(DesignProcessingArtifact).filter(
        DesignProcessingArtifact.status == "delete_pending"
    )
    if item_id is not None:
        query = query.filter(DesignProcessingArtifact.item_id == str(item_id))
    query = query.order_by(
        DesignProcessingArtifact.updated_at.asc(),
        DesignProcessingArtifact.id.asc(),
    )
    if limit is not None:
        query = query.limit(limit)
    artifact_ids = [
        artifact.id
        for artifact in query.all()
    ]
    deleted = 0
    failed = 0
    for artifact_id in artifact_ids:
        try:
            artifact_query = (
                db.query(DesignProcessingArtifact)
                .filter(DesignProcessingArtifact.id == artifact_id)
                .populate_existing()
            )
            if _supports_row_locks(db):
                artifact_query = artifact_query.with_for_update()
            artifact = artifact_query.one_or_none()
            if artifact is None or artifact.status != "delete_pending":
                db.rollback()
                continue
            item_query = db.query(DesignProcessingItem).filter(
                DesignProcessingItem.board_id == artifact.board_id,
                DesignProcessingItem.item_id == artifact.item_id,
            )
            if _supports_row_locks(db):
                item_query = item_query.with_for_update()
            item = item_query.one()
            replacement_identity = published_identity(item)
            artifact_identity = ProcessingIdentity(
                artifact.input_revision,
                artifact.pipeline_version,
            )
            if replacement_identity is None or replacement_identity == artifact_identity:
                raise DesignProcessingTargetMismatch(
                    "cleanup_replacement_missing",
                    "cleanup requires a different published replacement identity",
                )
            replacement_artifacts = _current_publication_artifacts(
                db,
                item,
                replacement_identity,
            )
            if any(
                replacement.status != "published"
                or replacement.monday_asset_id is None
                for replacement in replacement_artifacts
            ):
                raise DesignProcessingTargetMismatch(
                    "cleanup_replacement_incomplete",
                    "cleanup requires both replacement artifacts to remain published",
                )
            if cleanup_policy is not None and not cleanup_policy(artifact.item_id):
                log_design_processing_event(
                    logger,
                    "artifact_cleanup_blocked",
                    board_id=artifact.board_id,
                    item_id=artifact.item_id,
                    artifact_id=str(artifact.id),
                    artifact_kind=artifact.artifact_kind,
                )
                db.rollback()
                continue
            gateway.delete_design_file(
                artifact.board_id,
                artifact.item_id,
                artifact.column_id,
                str(artifact.monday_asset_id),
            )
            record_artifact_deleted(artifact, now=clock())
            db.commit()
            deleted += 1
            log_design_processing_event(
                logger,
                "artifact_cleanup_deleted",
                board_id=artifact.board_id,
                item_id=artifact.item_id,
                artifact_id=str(artifact.id),
                artifact_kind=artifact.artifact_kind,
            )
        except Exception as exc:
            db.rollback()
            artifact = db.get(DesignProcessingArtifact, artifact_id)
            if artifact is not None and artifact.status == "delete_pending":
                record_artifact_cleanup_error(
                    artifact,
                    error=str(exc),
                    now=clock(),
                )
                db.commit()
            failed += 1
            log_design_processing_event(
                logger,
                "artifact_cleanup_failed",
                level=logging.ERROR,
                artifact_id=str(artifact_id),
                error=str(exc),
            )
    return deleted, failed


def run_publication_pipeline(
    db: Session,
    job_id: object,
    *,
    worker_id: str,
    gateway: DesignProcessingReadGateway,
    artifact_storage: DesignArtifactStorage,
    pipeline_version: str,
    expected_board_id: str,
    expected_group_id: str,
    mode: DesignProcessingMode,
    allowlist_item_ids: Iterable[str] = (),
    execution_policy: Optional[ExecutionPolicy] = None,
    clock: Clock = utc_now,
) -> str:
    item, job, identity = _lock_current_publication(
        db,
        job_id,
        worker_id=worker_id,
    )
    extraction = item.extracted_parameters_json
    matching = item.match_result_json
    if not _has_identity(extraction, identity) or not _has_identity(matching, identity):
        raise RuntimeError("current publication outputs are unavailable")

    if job.stage == "writing_columns":
        parameters = dict(extraction.get("parameters") or {})
        db.commit()
        column_settings = gateway.fetch_design_owned_column_settings(item.board_id)
        column_values, warnings = build_design_owned_column_values(
            parameters,
            column_settings,
        )
        item, job, identity = _lock_current_publication(
            db,
            job_id,
            worker_id=worker_id,
        )
        assert_current_execution_target(
            item,
            job,
            gateway=gateway,
            pipeline_version=pipeline_version,
            expected_board_id=expected_board_id,
            expected_group_id=expected_group_id,
            worker_id=worker_id,
            execution_allowed=_execution_is_allowed(
                "publication",
                item.item_id,
                mode=mode,
                allowlist_item_ids=allowlist_item_ids,
                execution_policy=execution_policy,
            ),
        )
        if column_values:
            gateway.update_design_owned_columns(
                item.board_id,
                item.item_id,
                column_values,
            )
        _append_item_warnings(item, warnings)
        advance_stage(job, "uploading_ai_data", worker_id=worker_id, now=clock())
        db.commit()

    _publish_one_artifact(
        db,
        job_id,
        worker_id=worker_id,
        gateway=gateway,
        artifact_storage=artifact_storage,
        pipeline_version=pipeline_version,
        expected_board_id=expected_board_id,
        expected_group_id=expected_group_id,
        mode=mode,
        allowlist_item_ids=allowlist_item_ids,
        execution_policy=execution_policy,
        artifact_kind="ai_data",
        next_stage="uploading_match_report",
        clock=clock,
    )
    _publish_one_artifact(
        db,
        job_id,
        worker_id=worker_id,
        gateway=gateway,
        artifact_storage=artifact_storage,
        pipeline_version=pipeline_version,
        expected_board_id=expected_board_id,
        expected_group_id=expected_group_id,
        mode=mode,
        allowlist_item_ids=allowlist_item_ids,
        execution_policy=execution_policy,
        artifact_kind="match_report",
        next_stage=None,
        clock=clock,
    )

    item, job, identity = _lock_current_publication(
        db,
        job_id,
        worker_id=worker_id,
    )
    if not _execution_is_allowed(
        "publication",
        item.item_id,
        mode=mode,
        allowlist_item_ids=allowlist_item_ids,
        execution_policy=execution_policy,
    ):
        raise DesignProcessingTargetMismatch(
            "execution_disabled",
            "the current operational mode no longer permits publication completion",
        )
    artifacts = _current_publication_artifacts(db, item, identity)
    completed_at = clock()
    complete_publication(
        item,
        job,
        artifacts,
        worker_id=worker_id,
        now=completed_at,
    )
    mark_prior_artifacts_delete_pending(
        db,
        item,
        identity,
        now=completed_at,
    )
    db.commit()
    if _execution_is_allowed(
        "publication",
        item.item_id,
        mode=mode,
        allowlist_item_ids=allowlist_item_ids,
        execution_policy=execution_policy,
    ):
        cleanup_delete_pending_artifacts(
            db,
            gateway=gateway,
            cleanup_policy=lambda item_id: _execution_is_allowed(
                "publication",
                item_id,
                mode=mode,
                allowlist_item_ids=allowlist_item_ids,
                execution_policy=execution_policy,
            ),
            clock=clock,
        )
    return "published"
