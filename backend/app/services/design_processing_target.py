from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, Optional, Protocol, Sequence

from .. import monday_client
from ..models import DesignProcessingItem, DesignProcessingJob
from .design_processing_inputs import (
    DesignProcessingInputError,
    DesignProcessingTargetSnapshot,
    parse_design_processing_target,
)
from .design_processing_state import (
    ProcessingIdentity,
    desired_identity,
    execution_identity,
    update_desired_identity,
)


TargetReadiness = Literal[
    "ready",
    "waiting_for_name",
    "waiting_for_email",
    "ineligible",
]


class DesignProcessingReadGateway(Protocol):
    def fetch_target(self, item_id: str) -> DesignProcessingTargetSnapshot: ...

    def inspect_file_columns(
        self,
        item_id: str,
    ) -> dict[str, tuple[monday_client.MondayFileColumnAsset, ...]]: ...

    def fetch_project_word_hit_counts(
        self,
        words: Sequence[str],
    ) -> dict[str, int]: ...

    def fetch_project_items_matching_words(
        self,
        words: Sequence[str],
        *,
        start_date: str = "2021-01-01",
    ) -> tuple[monday_client.MondayProjectBoardItem, ...]: ...

    def fetch_project_items_matching_full_text(
        self,
        project_name: str,
        *,
        start_date: str = "2021-01-01",
    ) -> tuple[monday_client.MondayProjectBoardItem, ...]: ...

    def fetch_active_project_items_since(
        self,
        *,
        start_date: str = "2021-01-01",
    ) -> tuple[monday_client.MondayProjectBoardItem, ...]: ...

    def fetch_design_owned_column_settings(
        self,
        board_id: str,
    ) -> dict[str, str]: ...

    def update_design_owned_columns(
        self,
        board_id: str,
        item_id: str,
        column_values: Mapping[str, Any],
    ) -> None: ...

    def upload_design_file(
        self,
        item_id: str,
        column_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> monday_client.MondayFileColumnAsset: ...

    def delete_design_file(
        self,
        board_id: str,
        item_id: str,
        column_id: str,
        asset_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MondayDesignProcessingReadGateway:
    access_token: str
    project_board_id: str

    def fetch_target(self, item_id: str) -> DesignProcessingTargetSnapshot:
        raw_item = monday_client.fetch_design_processing_intake_item(
            self.access_token,
            item_id,
        )
        return parse_design_processing_target(raw_item)

    def inspect_file_columns(
        self,
        item_id: str,
    ) -> dict[str, tuple[monday_client.MondayFileColumnAsset, ...]]:
        return monday_client.inspect_design_processing_file_columns(
            self.access_token,
            item_id,
        )

    def fetch_project_word_hit_counts(
        self,
        words: Sequence[str],
    ) -> dict[str, int]:
        return monday_client.fetch_project_word_hit_counts(
            self.access_token,
            self.project_board_id,
            words,
        )

    def fetch_project_items_matching_words(
        self,
        words: Sequence[str],
        *,
        start_date: str = "2021-01-01",
    ) -> tuple[monday_client.MondayProjectBoardItem, ...]:
        return monday_client.fetch_project_items_matching_words(
            self.access_token,
            self.project_board_id,
            words,
            start_date=start_date,
        )

    def fetch_project_items_matching_full_text(
        self,
        project_name: str,
        *,
        start_date: str = "2021-01-01",
    ) -> tuple[monday_client.MondayProjectBoardItem, ...]:
        return monday_client.fetch_project_items_matching_full_text(
            self.access_token,
            self.project_board_id,
            project_name,
            start_date=start_date,
        )

    def fetch_active_project_items_since(
        self,
        *,
        start_date: str = "2021-01-01",
    ) -> tuple[monday_client.MondayProjectBoardItem, ...]:
        return monday_client.fetch_active_project_items_since(
            self.access_token,
            self.project_board_id,
            start_date=start_date,
        )

    def fetch_design_owned_column_settings(
        self,
        board_id: str,
    ) -> dict[str, str]:
        return monday_client.fetch_design_owned_column_settings(
            self.access_token,
            board_id,
        )

    def update_design_owned_columns(
        self,
        board_id: str,
        item_id: str,
        column_values: Mapping[str, Any],
    ) -> None:
        monday_client.update_design_owned_columns(
            self.access_token,
            board_id,
            item_id,
            column_values,
        )

    def upload_design_file(
        self,
        item_id: str,
        column_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> monday_client.MondayFileColumnAsset:
        return monday_client.upload_design_file(
            self.access_token,
            item_id,
            column_id,
            filename,
            content,
            content_type,
        )

    def delete_design_file(
        self,
        board_id: str,
        item_id: str,
        column_id: str,
        asset_id: str,
    ) -> None:
        monday_client.delete_design_file(
            self.access_token,
            board_id,
            item_id,
            column_id,
            asset_id,
        )


@dataclass(frozen=True, slots=True)
class RefreshedDesignProcessingTarget:
    snapshot: DesignProcessingTargetSnapshot
    readiness: TargetReadiness
    identity: Optional[ProcessingIdentity]


class DesignProcessingTargetMismatch(RuntimeError):
    def __init__(self, reason: str, detail: str):
        self.reason = reason
        super().__init__(detail)


def refresh_current_target(
    item: DesignProcessingItem,
    *,
    gateway: DesignProcessingReadGateway,
    pipeline_version: str,
    expected_board_id: str,
    expected_group_id: str,
    now: datetime,
    active_job: Optional[DesignProcessingJob] = None,
) -> RefreshedDesignProcessingTarget:
    snapshot = gateway.fetch_target(item.item_id)
    return apply_current_target_snapshot(
        item,
        snapshot,
        pipeline_version=pipeline_version,
        expected_board_id=expected_board_id,
        expected_group_id=expected_group_id,
        now=now,
        active_job=active_job,
    )


def apply_current_target_snapshot(
    item: DesignProcessingItem,
    snapshot: DesignProcessingTargetSnapshot,
    *,
    pipeline_version: str,
    expected_board_id: str,
    expected_group_id: str,
    now: datetime,
    active_job: Optional[DesignProcessingJob] = None,
) -> RefreshedDesignProcessingTarget:
    _assert_snapshot_item(snapshot, item)
    readiness = _target_readiness(
        snapshot,
        expected_board_id=expected_board_id,
        expected_group_id=expected_group_id,
    )
    identity = (
        ProcessingIdentity(snapshot.input_revision, pipeline_version)
        if readiness == "ready" and snapshot.input_revision is not None
        else None
    )
    changed = update_desired_identity(
        item,
        identity,
        now=now,
        active_job=active_job,
    )
    if readiness != "ready":
        item.state = readiness
        item.updated_at = now
    elif changed and item.state in {
        "waiting_for_name",
        "waiting_for_email",
        "ineligible",
        "failed",
    }:
        item.state = "scheduled"
        item.updated_at = now
    return RefreshedDesignProcessingTarget(
        snapshot=snapshot,
        readiness=readiness,
        identity=identity,
    )


def assert_current_execution_target(
    item: DesignProcessingItem,
    job: DesignProcessingJob,
    *,
    gateway: DesignProcessingReadGateway,
    pipeline_version: str,
    expected_board_id: str,
    expected_group_id: str,
    worker_id: str,
    execution_allowed: bool = True,
) -> DesignProcessingTargetSnapshot:
    snapshot = gateway.fetch_target(job.item_id)
    _assert_snapshot_item(snapshot, item)

    if job.status != "running" or job.locked_by != worker_id:
        raise DesignProcessingTargetMismatch(
            "lease_lost",
            "design-processing execution no longer owns its lease",
        )
    if not execution_allowed:
        raise DesignProcessingTargetMismatch(
            "execution_disabled",
            "the current operational mode does not permit this execution",
        )

    execution = execution_identity(job)
    if execution is None:
        raise DesignProcessingTargetMismatch(
            "execution_unassigned",
            "design-processing execution identity is not assigned",
        )
    if desired_identity(item) != execution:
        raise DesignProcessingTargetMismatch(
            "stored_identity_changed",
            "stored desired identity differs from the immutable execution identity",
        )
    if pipeline_version != execution.pipeline_version:
        raise DesignProcessingTargetMismatch(
            "pipeline_changed",
            "configured pipeline version differs from the execution identity",
        )

    readiness = _target_readiness(
        snapshot,
        expected_board_id=expected_board_id,
        expected_group_id=expected_group_id,
    )
    if readiness != "ready" or snapshot.input_revision is None:
        raise DesignProcessingTargetMismatch(
            readiness,
            f"current Monday target is {readiness}",
        )
    if snapshot.input_revision != execution.input_revision:
        raise DesignProcessingTargetMismatch(
            "input_changed",
            "current Monday input revision differs from the execution identity",
        )
    return snapshot


def _assert_snapshot_item(
    snapshot: DesignProcessingTargetSnapshot,
    item: DesignProcessingItem,
) -> None:
    if snapshot.item_id != str(item.item_id):
        raise DesignProcessingInputError(
            "monday returned a different item than the requested processing item"
        )
    if str(item.board_id) == "":
        raise DesignProcessingInputError("stored design-processing board ID is empty")


def _target_readiness(
    snapshot: DesignProcessingTargetSnapshot,
    *,
    expected_board_id: str,
    expected_group_id: str,
) -> TargetReadiness:
    if (
        snapshot.board_id != str(expected_board_id)
        or snapshot.group_id != str(expected_group_id)
    ):
        return "ineligible"
    if snapshot.missing_name:
        return "waiting_for_name"
    if snapshot.missing_email:
        return "waiting_for_email"
    return "ready"