from __future__ import annotations

import argparse
import json
import os
from typing import Any

from backend.app.config import settings
from backend.app.services.auto_sync import get_monday_ingestion_access_token
from backend.app.services.design_processing_inputs import (
    DownloadedDesignEmailAsset,
    download_design_email_assets,
)
from backend.app.services.design_processing_target import (
    MondayDesignProcessingReadGateway,
)
from backend.app.services.legacy_enquiry.analysis import (
    analyze_downloaded_email_assets,
)
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient


def inspect_design_parameter_extraction(item_id: str) -> dict[str, Any]:
    normalized_item_id = str(item_id).strip()
    if not normalized_item_id.isdecimal():
        raise ValueError("item_id must be a decimal Monday item ID")

    access_token = get_monday_ingestion_access_token()
    gateway = MondayDesignProcessingReadGateway(
        access_token=access_token,
        project_board_id=str(settings.design_processing_project_board_id),
    )
    snapshot = gateway.fetch_target(normalized_item_id)
    if not snapshot.email_assets:
        raise RuntimeError(
            f"Monday item {normalized_item_id} has no supported Email assets"
        )

    downloaded_assets: tuple[DownloadedDesignEmailAsset, ...] = ()
    try:
        downloaded_assets = download_design_email_assets(
            snapshot.email_assets,
            access_token,
        )
        result = analyze_downloaded_email_assets(
            downloaded_assets,
            client=LegacyGeminiClient(
                settings.design_processing_extraction_model,
                thinking_level=settings.design_processing_thinking_level,
            ),
        )
        return {
            "itemId": normalized_item_id,
            "boardId": snapshot.board_id,
            "groupId": snapshot.group_id,
            "itemState": snapshot.item_state,
            "inputRevision": snapshot.input_revision,
            "pipelineVersion": settings.design_processing_pipeline_version,
            "assetNames": [
                downloaded.source.filename for downloaded in downloaded_assets
            ],
            "projectName": result.project_name,
            "parameters": result.parameters,
        }
    finally:
        for downloaded in downloaded_assets:
            try:
                os.unlink(downloaded.temp_path)
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read a Monday intake item and print its extracted design parameters "
            "without queueing, persistence, artifact, matching, or Monday write calls."
        ),
    )
    parser.add_argument("--item-id", required=True)
    args = parser.parse_args()
    result = inspect_design_parameter_extraction(args.item_id)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
