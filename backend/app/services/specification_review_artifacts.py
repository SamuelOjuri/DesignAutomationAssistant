"""Deterministic review identities for publishing and exact-PDF approval.

Legacy artifacts intentionally have no review metadata. These helpers do not
upgrade them or render a PDF.
"""

from __future__ import annotations

import hashlib
import json

from .specification_review_contract import (
    FIELD_NAMES, ReviewDecisionState, ReviewSourceSnapshot, effective_field_value,
)


REVIEW_ARTIFACT_VERSION = "specification-review-artifact-v1"


def source_context_matches(state: ReviewDecisionState, source: ReviewSourceSnapshot) -> bool:
    return (
        (state.selections.selected_request_type is None or state.selections.selected_request_type == source.request_type)
        and state.selections.selected_project == source.selected_project
        and state.selections.selected_drawing == source.selected_drawing
    )


def review_digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def decision_digest(state: ReviewDecisionState) -> str:
    """Hash meaning and reviewed context, excluding timestamps, actors and version.

    Re-entering the same value is a no-op. Reconfirming it against changed sources
    is a meaningful change; explicit entry and acceptance remain distinct.
    """
    return review_digest({
        "schema_version": state.schema_version,
        "selections": state.selections.model_dump(mode="json"),
        "field_decisions": {
            label: {
                "decision": saved.decision.model_dump(mode="json"),
                "reviewed_source": saved.audit.reviewed_source.model_dump(mode="json"),
                "explanation": saved.audit.explanation,
            } for label, saved in state.field_decisions.items()
        },
        "source_dispositions": sorted([
            {
                "source_id": disposition.source_id,
                "issue_code": disposition.issue_code,
                "explanation": disposition.explanation,
                "reviewed_source": disposition.audit.reviewed_source.model_dump(mode="json"),
            } for disposition in state.source_dispositions
        ], key=lambda entry: (entry["source_id"], entry["issue_code"])),
    })


def build_review_artifact_metadata(
    state: ReviewDecisionState, source: ReviewSourceSnapshot,
    *, publication_version: str = REVIEW_ARTIFACT_VERSION,
) -> dict:
    """Return immutable metadata to store at artifact.source_json['review'].

    The publisher includes this metadata in its digest alongside model,
    template, effective PDF values and publication versions. Approval checks it
    verbatim rather than assuming that a historic PDF reflects saved decisions.
    """
    provenance = {}
    context_matches = source_context_matches(state, source)
    for label in FIELD_NAMES:
        proposal = source.fields[label].proposal
        saved = state.field_decisions.get(label)
        effective = effective_field_value(proposal, saved, current_source=source.identity)
        evidence = saved.decision.snapshot if saved and saved.decision.operation == "accept_recommendation" else proposal
        provenance[label] = {
            **effective.model_dump(mode="json"),
            "requires_reconfirmation": effective.requires_reconfirmation or (saved is not None and not context_matches),
            "source_label": effective.source_label,
            "email_evidence": evidence.model_dump(mode="json")["email_evidence"] if effective.source != "reviewer_specified" else [],
            "crm_evidence": evidence.model_dump(mode="json")["crm_evidence"] if effective.source != "reviewer_specified" else [],
            "derived_from": evidence.derived_from if effective.source != "reviewer_specified" else None,
        }
    metadata = {
        "schema_version": 1,
        "publication_version": publication_version,
        "review_version": state.review_version,
        "decision_digest": decision_digest(state),
        "source_identity": source.identity.model_dump(mode="json"),
        "recommendation_fingerprint": review_digest(source.model_dump(mode="json")),
        "selections": state.selections.model_dump(mode="json"),
        "recommendation_context": {
            "request_type": source.request_type,
            "selected_project": source.selected_project.model_dump(mode="json") if source.selected_project else None,
            "selected_drawing": source.selected_drawing.model_dump(mode="json") if source.selected_drawing else None,
        },
        "field_provenance": provenance,
    }
    return {**metadata, "publication_identity": review_digest(metadata)}
