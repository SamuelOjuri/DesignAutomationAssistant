from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session, defer

from ..models import DesignProcessingItem, DesignProcessingJob


def _supports_row_locks(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def lock_design_processing_item_and_job(
    db: Session,
    job_id: object,
    *,
    include_payloads: bool = True,
) -> tuple[Optional[DesignProcessingItem], Optional[DesignProcessingJob]]:
    """Lock item then job. State-only checks may skip the large JSON payloads.

    A stored extraction or retained failure payload is ~0.6 MB; cancellation
    checks run many times per execution and need only identity/lease columns.
    """
    locator = (
        db.query(DesignProcessingJob.board_id, DesignProcessingJob.item_id)
        .filter(DesignProcessingJob.id == job_id)
        .one_or_none()
    )
    if locator is None:
        return None, None

    item_query = (
        db.query(DesignProcessingItem)
        .filter(
            DesignProcessingItem.board_id == locator.board_id,
            DesignProcessingItem.item_id == locator.item_id,
        )
        .populate_existing()
    )
    if not include_payloads:
        item_query = item_query.options(
            defer(DesignProcessingItem.extracted_parameters_json),
            defer(DesignProcessingItem.match_result_json),
        )
    if _supports_row_locks(db):
        item_query = item_query.with_for_update()
    item = item_query.one_or_none()

    job_query = (
        db.query(DesignProcessingJob)
        .filter(DesignProcessingJob.id == job_id)
        .populate_existing()
    )
    if not include_payloads:
        job_query = job_query.options(defer(DesignProcessingJob.result_json))
    if _supports_row_locks(db):
        job_query = job_query.with_for_update()
    job = job_query.one_or_none()
    if job is not None and (
        job.board_id != locator.board_id or job.item_id != locator.item_id
    ):
        raise RuntimeError("design-processing job target changed while acquiring locks")
    return item, job
