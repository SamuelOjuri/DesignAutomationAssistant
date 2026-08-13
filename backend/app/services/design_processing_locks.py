from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from ..models import DesignProcessingItem, DesignProcessingJob


def _supports_row_locks(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def lock_design_processing_item_and_job(
    db: Session,
    job_id: object,
) -> tuple[Optional[DesignProcessingItem], Optional[DesignProcessingJob]]:
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
    if _supports_row_locks(db):
        item_query = item_query.with_for_update()
    item = item_query.one_or_none()

    job_query = (
        db.query(DesignProcessingJob)
        .filter(DesignProcessingJob.id == job_id)
        .populate_existing()
    )
    if _supports_row_locks(db):
        job_query = job_query.with_for_update()
    job = job_query.one_or_none()
    if job is not None and (
        job.board_id != locator.board_id or job.item_id != locator.item_id
    ):
        raise RuntimeError("design-processing job target changed while acquiring locks")
    return item, job
