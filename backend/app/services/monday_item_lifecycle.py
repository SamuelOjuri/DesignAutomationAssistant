"""Cancel work on an old registration after a verified cross-board move."""

from datetime import datetime

from ..models import DESIGN_PROCESSING_ACTIVE_JOB_STATUSES, DesignProcessingItem, DesignProcessingJob, Task


def cancel_out_of_board_work(db, board_id: str, item_id: str, *, now: datetime | None = None) -> None:
    """Retain documents/history while fencing workers tied to the original board.

    Call only after a current read has positively returned this item on another
    board. An empty/inaccessible lookup never establishes a board move.
    The caller owns the transaction.
    """
    from .auto_sync import cancel_active_auto_sync_jobs, lock_auto_sync_state, utc_now
    from .design_processing_state import cancel_job
    from .monday_metadata import cancel_metadata
    from .performance_specification import cancel_specification

    board_id, item_id = str(board_id), str(item_id)
    now = now or utc_now()
    # Match the publication/reviewer lock order: specification before job/task.
    cancel_specification(db, board_id, item_id, reason="board_not_managed")
    task_keys = [key for (key,) in db.query(Task.external_task_key).filter_by(
        board_id=board_id, item_id=item_id).order_by(Task.external_task_key).all()]
    for key in task_keys:
        jobs, task = lock_auto_sync_state(db, board_id=board_id, item_id=item_id, external_task_key=key)
        if task is None:
            continue
        cancel_active_auto_sync_jobs(db, board_id=board_id, item_id=item_id,
            reason="board_not_managed", now=now, existing_jobs=jobs, active_job_lookup_complete=True)
        task.auto_sync_state, task.auto_sync_enabled = "excluded", False
        task.purge_after = None
        task.updated_at = now
        if task.sync_status in {"queued", "syncing"}:
            task.sync_status = "completed"
            task.sync_finished_at = task.sync_completed_at = now
            task.sync_error, task.last_sync_result = None, "skipped"
        cancel_metadata(db, task)
    jobs_query = db.query(DesignProcessingJob).filter(
        DesignProcessingJob.board_id == board_id, DesignProcessingJob.item_id == item_id,
        DesignProcessingJob.status.in_(DESIGN_PROCESSING_ACTIVE_JOB_STATUSES),
    ).populate_existing().order_by(DesignProcessingJob.id)
    if db.get_bind().dialect.name == "postgresql":
        jobs_query = jobs_query.with_for_update()
    jobs = jobs_query.all()
    item_query = db.query(DesignProcessingItem).filter_by(board_id=board_id, item_id=item_id).populate_existing()
    if db.get_bind().dialect.name == "postgresql":
        item_query = item_query.with_for_update()
    item = item_query.one_or_none()
    if item is not None:
        for job in jobs:
            cancel_job(item, job, reason="board_not_managed", now=now, item_state="ineligible")
        item.state, item.updated_at = "ineligible", now
