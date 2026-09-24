from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from ..config import settings


EXECUTION_STATES = ("queued", "syncing", "completed", "failed")
SYNC_RESULTS = ("done", "unchanged", "skipped", "failed")
INACTIVE_SOURCE_STATES = ("archived", "deleted")
LIFECYCLE_STATES = (
    "active",
    *INACTIVE_SOURCE_STATES,
    "completed_retained",
    "purge_pending",
    "storage_deleting",
    "database_cleaning",
    "expired",
    "excluded",
    "paused",
)
JOB_STATUSES = (
    "pending",
    "scheduled",
    "running",
    "retry_wait",
    "completed",
    "skipped",
    "failed",
    "cancelled",
)
ACTIVE_JOB_STATUSES = ("pending", "scheduled", "running", "retry_wait")


@dataclass(frozen=True)
class AutoSyncDecision:
    board_id: str
    group_id: Optional[str]
    lifecycle_state: Optional[str]
    should_track_task: bool
    should_queue_sync: bool
    should_cancel_active_jobs: bool
    reason: str
    requires_existing_index: bool = False


@dataclass(frozen=True)
class AutoSyncPolicy:
    enabled: bool
    board_id: str
    active_group_ids: frozenset[str]
    excluded_group_ids: frozenset[str]
    completed_group_id: str
    retention_days: int
    debounce_seconds: int
    backfill_batch_size: int

    def classify_item(self, board_id: str, group_id: Optional[str], state: Optional[str]) -> AutoSyncDecision:
        """Monday retains group IDs on archived items; state takes precedence."""
        if str(board_id) != self.board_id:
            return self.classify_group(board_id, group_id)
        if state in INACTIVE_SOURCE_STATES:
            return AutoSyncDecision(
                board_id=str(board_id), group_id=str(group_id) if group_id is not None else None,
                lifecycle_state=state, should_track_task=True, should_queue_sync=False,
                should_cancel_active_jobs=True, reason=f"item_{state}", requires_existing_index=True,
            )
        if state != "active":
            raise ValueError("Monday item state is missing or unsupported")
        return self.classify_group(board_id, group_id)

    def classify_group(self, board_id: str, group_id: Optional[str]) -> AutoSyncDecision:
        normalized_board_id = str(board_id)
        normalized_group_id = str(group_id) if group_id is not None else None

        if normalized_board_id != self.board_id:
            return AutoSyncDecision(
                board_id=normalized_board_id,
                group_id=normalized_group_id,
                lifecycle_state=None,
                should_track_task=False,
                should_queue_sync=False,
                should_cancel_active_jobs=False,
                reason="board_not_managed",
            )

        if normalized_group_id in self.excluded_group_ids:
            return AutoSyncDecision(
                board_id=normalized_board_id,
                group_id=normalized_group_id,
                lifecycle_state="excluded",
                should_track_task=True,
                should_queue_sync=False,
                should_cancel_active_jobs=True,
                reason="excluded_group",
            )

        if normalized_group_id in self.active_group_ids:
            return AutoSyncDecision(
                board_id=normalized_board_id,
                group_id=normalized_group_id,
                lifecycle_state="active",
                should_track_task=True,
                should_queue_sync=self.enabled,
                should_cancel_active_jobs=False,
                reason="active_group" if self.enabled else "auto_sync_disabled",
            )

        if normalized_group_id == self.completed_group_id:
            return AutoSyncDecision(
                board_id=normalized_board_id,
                group_id=normalized_group_id,
                lifecycle_state="completed_retained",
                should_track_task=True,
                should_queue_sync=False,
                should_cancel_active_jobs=False,
                reason="completed_retention_only",
                requires_existing_index=True,
            )

        return AutoSyncDecision(
            board_id=normalized_board_id,
            group_id=normalized_group_id,
            lifecycle_state=None,
            should_track_task=False,
            should_queue_sync=False,
            should_cancel_active_jobs=False,
            reason="unknown_group",
        )

    def purge_after_for(self, completed_at: Optional[datetime] = None) -> datetime:
        base = completed_at or datetime.now(timezone.utc)
        return base + timedelta(days=self.retention_days)


def _group_ids(value: Iterable[str] | str) -> frozenset[str]:
    if isinstance(value, str):
        value = value.split(",")
    return frozenset(str(group_id).strip() for group_id in value if str(group_id).strip())


def policy_from_settings() -> AutoSyncPolicy:
    return AutoSyncPolicy(
        enabled=settings.auto_sync_enabled,
        board_id=str(settings.auto_sync_board_id),
        active_group_ids=_group_ids(settings.auto_sync_active_group_ids),
        excluded_group_ids=_group_ids(settings.auto_sync_excluded_group_ids),
        completed_group_id=str(settings.auto_sync_completed_group_id),
        retention_days=settings.auto_sync_retention_days,
        debounce_seconds=settings.auto_sync_debounce_seconds,
        backfill_batch_size=settings.auto_sync_backfill_batch_size,
    )


def build_external_task_key(account_id: str, board_id: str, item_id: str) -> str:
    return f"{account_id}:{board_id}:{item_id}"
