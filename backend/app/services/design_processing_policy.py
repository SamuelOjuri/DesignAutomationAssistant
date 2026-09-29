"""Group policy shared by admission, execution, recovery and cleanup."""

from ..config import settings
from .design_processing_inputs import DesignProcessingTargetSnapshot


def active_design_group_ids() -> frozenset[str]:
    # Reuse the board's group inventory, independently of the auto-sync switches.
    return frozenset(
        value.strip()
        for value in settings.auto_sync_active_group_ids.split(",")
        if value.strip()
    )


def design_scope_exclusion(
    snapshot: DesignProcessingTargetSnapshot,
    *,
    expected_board_id: str,
    landing_group_id: str,
    registered: bool,
) -> str | None:
    if snapshot.board_id != str(expected_board_id):
        return "board_not_managed"
    if snapshot.group_id == str(settings.auto_sync_completed_group_id):
        return "completed_folder"
    if snapshot.item_state != "active":
        return f"item_{snapshot.item_state}"
    if snapshot.group_id == str(landing_group_id):
        return None
    if snapshot.group_id in active_design_group_ids():
        return None if registered else "not_registered_in_landing_zone"
    return "group_not_eligible"
