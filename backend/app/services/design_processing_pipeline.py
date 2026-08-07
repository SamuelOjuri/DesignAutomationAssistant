from __future__ import annotations

from datetime import datetime
import os
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy.orm import Session

from ..models import DesignProcessingItem, DesignProcessingJob
from .auto_sync import utc_now
from .design_processing_artifacts import (
    DesignArtifactStorage,
    find_verified_rendered_artifacts,
    persist_rendered_artifact,
    render_analysis_artifacts,
)
from .design_processing_inputs import (
    DownloadedAssetLike,
    download_design_email_assets,
)
from .design_processing_queue import (
    DesignProcessingMode,
    publication_allowed_for_item,
    queue_design_processing_snapshot,
)
from .design_processing_state import (
    ProcessingIdentity,
    advance_stage,
    complete_analysis,
    desired_identity,
    execution_identity,
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
from .legacy_enquiry.matching import build_matching_contract, match_projects
from .match_report import MatchReport


Clock = Callable[[], datetime]
AssetDownloader = Callable[[dict[str, Any], str], DownloadedAssetLike]


def _supports_row_locks(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def _lock_current_analysis(
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
    downloader: Optional[AssetDownloader] = None,
    clock: Clock = utc_now,
) -> str:
    item, job, identity = _lock_current_analysis(
        db,
        job_id,
        worker_id=worker_id,
    )
    snapshot = assert_current_execution_target(
        item,
        job,
        gateway=gateway,
        pipeline_version=pipeline_version,
        expected_board_id=expected_board_id,
        expected_group_id=expected_group_id,
        worker_id=worker_id,
        execution_allowed=mode != "off",
    )
    db.rollback()

    item, job, identity = _lock_current_analysis(
        db,
        job_id,
        worker_id=worker_id,
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

    publication_allowed = publication_allowed_for_item(
        mode=mode,
        item_id=item.item_id,
        allowlist_item_ids=allowlist_item_ids,
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
