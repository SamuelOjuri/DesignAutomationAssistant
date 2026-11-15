"""Specification scope, independent of active-project ingestion and chat.

All callers share the canonical design board, Landing Zone and active groups.
Completed Folder always wins if the group settings accidentally overlap.
"""

from ..config import settings
from .design_processing_policy import eligible_design_group_ids

SPECIFICATION_SCOPE_POLICY_VERSION = "specification-active-review-v2"


def specification_rollout_exclusion(item_id) -> str | None:
    """Item rollout gate, without the publisher's temporary master pause.

    Reviewers can still save decisions while publication is paused. Design
    extraction/output mode and its allowlist do not grant specification access.
    """
    mode = settings.performance_specification_mode
    if mode not in {"shadow", "allowlist", "enabled"}:
        return "processing_off"
    if mode == "allowlist" and str(item_id) not in settings.performance_specification_allowlist_item_ids:
        return "item_not_allowlisted"
    return None


def specification_processing_enabled() -> bool:
    """Whether specification work may run at all under its own controls."""
    return bool(settings.performance_specification_enabled and settings.performance_specification_mode in {
        "shadow", "allowlist", "enabled",
    })


def specification_processing_allowed(item_id) -> bool:
    return specification_processing_enabled() and specification_rollout_exclusion(item_id) is None


def specification_publication_allowed(item_id) -> bool:
    """Private shadow rendering cannot publish a Monday file or completion."""
    return specification_processing_allowed(item_id) and settings.performance_specification_mode in {
        "allowlist", "enabled",
    }


def eligible_specification_group_ids() -> frozenset[str]:
    return eligible_design_group_ids(landing_group_id=str(settings.design_processing_landing_group_id))


def specification_publication_mode(snapshot: dict) -> str:
    """Describe the live workflow; operational/actor permissions remain separate."""
    group, board = snapshot.get("group"), snapshot.get("board")
    group_id = group.get("id") if isinstance(group, dict) else None
    if specification_scope_exclusion(board_id=board.get("id") if isinstance(board, dict) else None,
            group_id=group_id, item_state=snapshot.get("state")) is not None:
        return "disabled"
    return "automatic" if str(group_id) == str(settings.design_processing_landing_group_id) else "manual"


def specification_scope_exclusion(
    snapshot=None, *, board_id=None, group_id=None, item_state=None,
) -> str | None:
    """Return a stable exclusion reason for a live target or its identity fields.

    Queue admission may check board/group before a live read. Execution and
    publication must supply the live lifecycle as well; a missing state fails
    closed here rather than implying that an item is active.
    """
    if snapshot is not None:
        board_id, group_id, item_state = snapshot.board_id, snapshot.group_id, snapshot.item_state
    if str(board_id) != str(settings.design_processing_board_id):
        return "board_not_managed"
    if str(group_id) == str(settings.auto_sync_completed_group_id):
        return "completed_folder"
    if item_state != "active":
        return f"item_{item_state or 'unknown'}"
    if str(group_id) not in eligible_specification_group_ids():
        return "group_not_eligible"
    return None
