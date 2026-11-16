"""A bounded project lookup must remain visibly incomplete through review."""

import io
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from backend.app.services.match_report import MatchReport, render_match_report_pdf
from backend.app.services.performance_specification_sources import prepare_specification_candidates


def contract(*, candidates=True):
    return {
        "extractedProjectTitle": "Pomeroy Street",
        "titleLookup": {"status": "available"},
        "candidates": [{
            "rank": 1, "mondayItemId": "1779783517", "projectReference": "3861",
            "projectTitle": "Pomeroy Street", "similarity": 1.0,
            "matchPercentage": "100.0%", "createdDate": "2018-10-16",
            "matchKind": "exact_title", "matchRationale": "Project title matches the request",
        }] if candidates else [],
    }


def read_report(payload):
    report = MatchReport.from_contract(
        source_item_id="3360774097", extracted_company="Fatra", match_contract=payload,
    )
    document = PdfReader(io.BytesIO(render_match_report_pdf(report)))
    text = "\n".join(page.extract_text() for page in document.pages)
    for page in document.pages:
        positions = []
        page.extract_text(visitor_text=lambda text, cm, tm, font, size:
            positions.append(tm[5]) if text.strip() else None)
        assert all(30 <= position <= float(page.mediabox.height) - 30 for position in positions)
    return text


@pytest.mark.parametrize("lookup,status", [
    ("titleLookup", "incomplete"), ("titleLookup", "unavailable"),
    ("referenceLookup", "unavailable"),
])
def test_empty_incomplete_report_does_not_conclude_no_matches(lookup, status):
    payload = contract(candidates=False)
    payload[lookup] = {"status": status, "detail": "private upstream data"}
    text = read_report(payload)
    assert "Project lookup incomplete" in text
    assert "Potential matches shown: 0" in text
    assert "No candidates are available from this incomplete lookup." in text
    assert "No matches found." not in text
    assert "private upstream data" not in text


def test_incomplete_report_retains_readable_candidates_without_claiming_total():
    payload = contract()
    payload["titleLookup"] = {"status": "incomplete"}
    text = read_report(payload)
    assert "Project lookup incomplete" in text
    assert "Potential matches shown: 1" in text
    assert "Candidate TP Ref: 3861" in text
    assert "Total potential matches" not in text


def test_display_truncation_is_distinct_from_incomplete_remote_search():
    payload = contract()
    payload["searchDiagnostics"] = {
        "totalQualifiedCandidateCount": 501, "returnedCandidateCount": 1, "truncated": True,
    }
    text = read_report(payload)
    assert "Potential matches shown: 1" in text
    assert "501 candidates qualified in the retrieved results." in text
    assert "Additional qualifying candidates are omitted." in text
    assert "Project lookup incomplete" not in text


def test_complete_empty_report_keeps_empty_result_distinct():
    text = read_report(contract(candidates=False))
    assert "Total potential matches: 0" in text
    assert "No matches found." in text
    assert "Project lookup incomplete" not in text


@pytest.mark.parametrize("candidate_count", [0, 1])
def test_incomplete_title_lookup_keeps_review_context_pending(candidate_count):
    payload = contract(candidates=bool(candidate_count))
    payload["titleLookup"] = {"status": "incomplete"}

    class Gateway:
        def fetch_specification_project(self, identity):
            return {"id": identity, "name": "3861", "state": "active", "board": {"id": "700"},
                    "column_values": [], "subitems": []}

    snapshot = prepare_specification_candidates(Gateway(), payload, {}, project_board_id="700")
    assert snapshot["lookup"]["status"] == "pending"
    assert len(snapshot["lookup"]["projects"]) == candidate_count
    assert any("title lookup is incomplete" in issue for issue in snapshot["lookup"]["issues"])


def test_display_truncation_keeps_review_context_pending_even_with_few_shown():
    payload = contract(candidates=False)
    payload["searchDiagnostics"] = {"truncated": True, "totalQualifiedCandidateCount": 501}
    snapshot = prepare_specification_candidates(SimpleNamespace(), payload, {}, project_board_id="700")
    assert snapshot["lookup"]["status"] == "pending"
    assert any("qualifying projects are omitted" in issue for issue in snapshot["lookup"]["issues"])


def test_unavailable_reference_lookup_takes_priority_over_pending_title_lookup():
    payload = contract(candidates=False)
    payload["titleLookup"] = {"status": "incomplete"}
    payload["referenceLookup"] = {"status": "unavailable"}
    snapshot = prepare_specification_candidates(SimpleNamespace(), payload, {}, project_board_id="700")
    assert snapshot["lookup"]["status"] == "unavailable"
