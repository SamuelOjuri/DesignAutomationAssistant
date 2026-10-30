"""Evidence matching regressions using synthetic source text only."""

from copy import deepcopy

import pytest

from backend.app.services.performance_specification_contract import FIELD_NAMES
from backend.app.services.performance_specification_evidence import resolve_email_evidence
from backend.app.services.specification_semantics import EVIDENCE_VERSION


SENT = "Fri, 30 Oct 2026 11:32:17 +0000"
OBSERVED = "2026-10-30T11:32:17+00:00"


def source(identity, text, kind="attachment"):
    return {"sourceId": identity, "sourceType": kind, "filename": "synthetic",
            "contentSha256": "abc", "status": "read", "text": text, "observedAt": OBSERVED}


def candidate(field, value, snippet, **overrides):
    return {"field": field, "value": value, "source_id": "asset:1:attachment:0",
            "location": "title block", "snippet": snippet, "scope": None, "issuer": None,
            "role": "requested", "request_role": "document", "message_date": None,
            "rationale": "Explicit source fact", **overrides}


def extraction(sources, candidates):
    return {"specificationEvidenceVersion": EVIDENCE_VERSION,
            "specificationAssessedFields": list(FIELD_NAMES), "performanceParameters": {},
            "sourceManifest": sources, "specificationEvidence": candidates}


@pytest.mark.parametrize("text,snippet", [
    ("* **Scale:** 1:100 @ A1", "Scale: 1:100 @ A1"),
    ("Scale: `1:100` @ A1", "Scale: 1:100 @ A1"),
    ("Scale: 1:100 @ A1", "**Scale:** 1:100 @ A1"),
])
def test_presentation_markers_do_not_invalidate_contiguous_excerpts(text, snippet):
    data = extraction([source("asset:1:attachment:0", text)], [
        candidate("Scale", "1:100", snippet), candidate("Page Size", "A1", snippet),
    ])
    raw, _, issues = resolve_email_evidence(data)
    assert raw["Scale"] == "1:100" and raw["Page Size"] == "A1" and not issues


@pytest.mark.parametrize("text,snippet,value", [
    ("Scale: 1:100", "Scale: 1:10", "1:100"),  # Changed numeric fact.
    ("Scale: 1:100", "Scale: 1:10", "1:10"),  # Truncated source excerpt.
    ("Scale: 1:100", "Scale: 1:100", "1:10"),  # Truncated value.
    ("Scale: 1:100\nOther information\nPage: A1", "Scale: 1:100 ... Page: A1", "1:100"),
    ("Scale: 1:100\nOther information\nPage: A1", "Scale: 1:100 Page: A1", "1:100"),
    ("Scale: 1/100", "Scale: 1:100", "1:100"),  # Punctuation is evidence too.
    ("Scale: 1:100", "** **", "1:100"),
])
def test_matching_does_not_accept_invented_or_spliced_facts(text, snippet, value):
    data = extraction([source("asset:1:attachment:0", text)], [candidate("Scale", value, snippet)])
    raw, _, issues = resolve_email_evidence(data)
    assert raw["Scale"] is None and "Scale" in issues


@pytest.mark.parametrize("reverse", [False, True])
def test_verified_recovery_replaces_only_the_bad_excerpt_for_the_same_claim(reverse):
    good = candidate("Bauder Contract Number", "B123456", "Subject: Bauder B123456")
    bad = {**good, "snippet": "Subject: Bauder B123456 ... Contract B123456"}
    data = extraction([source(good["source_id"], good["snippet"])], [bad, good][::(-1 if reverse else 1)])
    raw, provenance, issues = resolve_email_evidence(data)
    assert raw["Bauder Contract Number"] == "B123456" and not issues
    rejected = provenance["Bauder Contract Number"]["rejectedCandidates"]
    assert rejected[0]["snippet"] == bad["snippet"] and rejected[0]["status"] == "repaired"


@pytest.mark.parametrize("change", [
    {"value": "B654321"}, {"source_id": "missing"}, {"scope": "Roof 5"},
    {"source_id": "ASSET:1:attachment:0"},
    {"request_role": "forwarded_request"}, {"issuer": "Another issuer"},
    {"message_date": "15 October 2026 09:00"},
])
def test_verified_fact_does_not_hide_an_unverified_different_claim(change):
    good = candidate("Bauder Contract Number", "B123456", "Subject: Bauder B123456")
    bad = {**good, "snippet": "Unverified excerpt", **change}
    data = extraction([source(good["source_id"], good["snippet"])], [bad, good])
    _, provenance, issues = resolve_email_evidence(data)
    assert "Bauder Contract Number" in issues
    assert provenance["Bauder Contract Number"]["rejectedCandidates"][0]["status"] == "unverified"


@pytest.mark.parametrize("identity", ["asset:1:attachment:0", "asset:1:attachment:0:member:2"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_attachment_can_cite_its_own_verified_outer_request_timestamp(identity, newline):
    text = "A3=Project Name: | F3=Example Roof"
    data = extraction([
        source("asset:1:body", f"From: Requester\nDate: {SENT}\n\nRequest".replace("\n", newline), "email_body"),
        source(identity, text),
    ], [candidate("Drawing Title", "Example Roof", text, source_id=identity, message_date=SENT)])
    raw, provenance, issues = resolve_email_evidence(data)
    assert raw["Drawing Title"] == "Example Roof" and not issues
    assert provenance["Drawing Title"]["selected"]["observedAt"] == OBSERVED


@pytest.mark.parametrize("case", ["unrelated", "forwarded_date", "mismatched_observed", "forwarded_role", "standalone"])
def test_unverified_attachment_timestamp_still_blocks(case):
    text = "Project: Example Roof"
    parent = source("asset:1:body", f"From: Requester\nDate: {SENT}\n\nRequest", "email_body")
    attachment = source("asset:1:attachment:0", text)
    evidence = candidate("Drawing Title", "Example Roof", text, message_date=SENT)
    if case == "unrelated":
        parent["sourceId"] = "asset:2:body"
    elif case == "forwarded_date":
        parent["text"] = f"From: Requester\nDate: Thu, 29 Oct 2026 11:32:17 +0000\n\nForwarded:\nDate: {SENT}"
    elif case == "mismatched_observed":
        attachment["observedAt"] = "2026-10-29T11:32:17+00:00"
    elif case == "forwarded_role":
        evidence["request_role"] = "forwarded_request"
    elif case == "standalone":
        evidence["source_id"] = attachment["sourceId"] = "asset:2"
    raw, _, issues = resolve_email_evidence(extraction([parent, attachment], [evidence]))
    assert raw["Drawing Title"] is None
    assert issues["Drawing Title"] == "Candidate request timestamp could not be verified"


def test_readable_table_preserves_roof_conflict_and_requires_confirmed_scope():
    text = "| **Roof 1 (Top)** | Timber (18mm) |\n| **Roof 5 (Front)** | Concrete |"
    data = extraction([source("asset:1:attachment:0", text)], [
        candidate("Decking", "Timber (18mm)", "Roof 1 (Top) | Timber (18mm)", scope="Roof 1 (Top)"),
        candidate("Decking", "Concrete", "Roof 5 (Front) | Concrete", scope="Roof 5 (Front)"),
    ])
    raw, provenance, issues = resolve_email_evidence(data)
    assert raw["Decking"] is None and len(provenance["Decking"]["candidates"]) == 2
    assert issues == {"Decking": "Conflicting roof/drawing/request values require a confirmed target"}
    raw, _, issues = resolve_email_evidence(deepcopy(data), ["Roof 5 (Front)"])
    assert raw["Decking"] == "Concrete" and not issues
