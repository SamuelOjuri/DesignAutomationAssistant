"""Resolve an extraction against a local, deliberately empty CRM context.

This calls the production source resolver and metadata builder only. It does
not import worker execution, open a database, call models, or access Monday.
The synthetic identity exists solely to satisfy Landing Zone input guards.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import settings
from backend.app.services.performance_specification_contract import (
    SPECIFICATION_VERSION, digest, specification_translation_inputs,
)
from backend.app.services.performance_specification_sources import (
    SpecificationReviewRequired, resolve_specification_sources,
)
from backend.app.services.specification_review_artifacts import build_review_artifact_metadata
from backend.app.services.specification_review_contract import (
    ReviewDecisionState, ReviewSourceSnapshot,
)


class LocalGateway:
    """Only synthetic scope and input reads; CRM lookups explicitly unavailable."""

    def __init__(self, revision: str):
        self.target = SimpleNamespace(
            board_id=str(settings.design_processing_board_id), item_id="1",
            group_id=str(settings.design_processing_landing_group_id),
            item_state="active", input_revision=revision,
        )

    def fetch_target(self, item_id):
        if str(item_id) != "1":
            raise ValueError("Unexpected synthetic target")
        return deepcopy(self.target)

    def fetch_holding(self, item_id):
        if str(item_id) != "1":
            raise ValueError("Unexpected synthetic holding item")
        return {
            "id": "1", "name": "Local attachment preview (synthetic)",
            "board": {"id": self.target.board_id},
            "group": {"id": self.target.group_id},
            "state": "active", "column_values": [],
        }

    def fetch_project(self, item_id):
        raise ValueError("No verified CRM project supplied to this local preview")

    def resolve_person(self, kind, entity_id):
        raise ValueError("No verified CRM directory supplied to this local preview")


def resolve_payload(extraction: dict, *, review_state=None) -> dict:
    """Return production resolution, metadata, and exact translation inputs.

    A review-required result includes the source snapshot that the worker would
    retain, but is explicitly marked as unable to proceed to PDF rendering.
    A successful source resolution can still contain printable field conflicts;
    those differ from the blocking evidence failures raised by the resolver.
    """
    payload = deepcopy(extraction)
    payload.setdefault("inputRevision", "local-attachment-preview")
    payload.setdefault("pipelineVersion", settings.design_processing_pipeline_version)
    pipeline = payload["pipelineVersion"]
    revision = payload["inputRevision"]
    state = (review_state if isinstance(review_state, ReviewDecisionState)
             else ReviewDecisionState.model_validate(review_state or {}))
    gateway = LocalGateway(revision)
    item = SimpleNamespace(
        board_id=gateway.target.board_id, item_id="1",
        latest_desired_input_revision=revision,
        latest_analyzed_input_revision=revision,
        latest_desired_pipeline_version=pipeline,
        latest_analyzed_pipeline_version=pipeline,
        extracted_parameters_json=payload,
        match_result_json={},
    )
    blocked = False
    error = None
    try:
        source = resolve_specification_sources(gateway, item, review_state=state)
    except SpecificationReviewRequired as exc:
        source = exc.source
        blocked = True
        error = str(exc)
    snapshot = ReviewSourceSnapshot.model_validate(source["reviewSource"])
    source["review"] = build_review_artifact_metadata(state, snapshot)
    source["publicationVersion"] = SPECIFICATION_VERSION
    inputs = specification_translation_inputs(source)
    source["translationIdentity"] = digest(inputs)
    return {
        "localPreview": True,
        "assumptions": [
            "Both local files supplied in the same extraction inventory",
            "Synthetic active Landing Zone identity; no actual Monday item checked",
            "No project matches, CRM values, or saved reviewer decisions supplied",
            "No database, Monday, storage, or model writes performed by this resolver",
        ],
        "sourceResolutionStatus": "review_required" if blocked else "resolved",
        "workerCanProceedToRender": not blocked,
        "blockingError": error,
        "raw": source["raw"],
        "issues": source["issues"],
        "fieldProvenance": source["review"]["field_provenance"],
        "translationInputs": inputs,
        "source": source,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = resolve_payload(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "sourceResolutionStatus", "workerCanProceedToRender", "blockingError", "raw", "issues",
    )}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
