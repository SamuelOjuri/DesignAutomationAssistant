from __future__ import annotations

from dataclasses import dataclass
import io
from typing import Any, Mapping, Optional

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .legacy_enquiry.formatting import clean_extracted_value


@dataclass(frozen=True, slots=True)
class MatchReportCandidate:
    rank: int
    monday_item_id: str
    project_reference: str
    project_title: str
    similarity: float
    match_percentage: str
    created_date: Optional[str] = None
    match_rationale: str = ""


@dataclass(frozen=True, slots=True)
class MatchReport:
    source_item_id: str
    extracted_project_title: str
    extracted_company: str
    candidates: tuple[MatchReportCandidate, ...]
    request_recommendation: str = "Reviewer decision required"
    intent_explanation: str = ""

    @classmethod
    def from_contract(
        cls,
        *,
        source_item_id: str,
        extracted_company: str,
        match_contract: Mapping[str, Any],
    ) -> "MatchReport":
        candidates = tuple(
            MatchReportCandidate(
                rank=int(candidate["rank"]),
                monday_item_id=str(candidate["mondayItemId"]),
                project_reference=str(candidate["projectReference"]),
                project_title=str(candidate["projectTitle"]),
                similarity=float(candidate["similarity"]),
                match_percentage=str(candidate["matchPercentage"]),
                created_date=(
                    str(candidate["createdDate"])
                    if candidate.get("createdDate")
                    else None
                ),
                match_rationale=str(candidate.get("matchRationale") or ""),
            )
            for candidate in match_contract.get("candidates", ())
        )
        intent = match_contract.get("specificationRequestIntent")
        from .specification_intent import reason_for_change
        return cls(
            source_item_id=str(source_item_id),
            extracted_project_title=str(
                match_contract.get("extractedProjectTitle") or "Not found"
            ),
            extracted_company=str(
                clean_extracted_value(extracted_company) or "Not found"
            ),
            candidates=candidates,
            request_recommendation=reason_for_change(intent) if intent else "Reviewer decision required",
            intent_explanation=str(intent.get("explanation") or "") if intent else "",
        )


def _wrapped_lines(
    text: str,
    *,
    font_name: str,
    font_size: float,
    max_width: float,
) -> list[str]:
    words = str(text).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_match_report_pdf(report: MatchReport) -> bytes:
    buffer = io.BytesIO()
    page_width, page_height = A4
    document = canvas.Canvas(
        buffer,
        pagesize=A4,
        pageCompression=0,
        invariant=1,
    )
    left = 48
    right = page_width - 48
    top = page_height - 48
    bottom = 48
    y = top

    def new_page() -> None:
        nonlocal y
        document.showPage()
        y = top

    def write(
        text: str,
        *,
        font_name: str = "Helvetica",
        font_size: float = 10,
        leading: float = 14,
        gap_after: float = 0,
    ) -> None:
        nonlocal y
        lines = _wrapped_lines(
            text,
            font_name=font_name,
            font_size=font_size,
            max_width=right - left,
        )
        for line in lines:
            if y - leading < bottom:
                new_page()
            document.setFont(font_name, font_size)
            document.drawString(left, y, line)
            y -= leading
        y -= gap_after

    def reserve(lines: list[tuple[str, str]]) -> None:
        """Keep an ordinary candidate or action block on one page when it fits."""
        height = 8 + sum(14 * len(_wrapped_lines(text, font_name=font,
            font_size=10, max_width=right - left)) for text, font in lines)
        if y - min(height, top - bottom) < bottom:
            new_page()

    write(
        "Matched Projects Review",
        font_name="Helvetica-Bold",
        font_size=18,
        leading=22,
        gap_after=8,
    )
    write(f"Source Monday Item: {report.source_item_id}")
    write(f"Extracted Project Title: {report.extracted_project_title}")
    write(
        "Extracted Company Name (context only for the reviewer's Accounts decision; "
        f"not a resolved Monday account): {report.extracted_company}",
        gap_after=8,
    )
    write(f"Request type: {report.request_recommendation}", font_name="Helvetica-Bold")
    if report.intent_explanation:
        write(report.intent_explanation)
    write("Project matches support review. Title similarity is not a probability of an amendment.", gap_after=8)
    write(
        f"Total potential matches: {len(report.candidates)}",
        font_name="Helvetica-Bold",
        gap_after=8,
    )

    if not report.candidates:
        write("No matches found.", font_name="Helvetica-Bold", gap_after=8)
    else:
        for candidate in report.candidates:
            reserve([(f"{candidate.rank}. {candidate.project_title}", "Helvetica-Bold"),
                (f"Candidate TP Ref: {candidate.project_reference}", "Helvetica"),
                (f"Title similarity: {candidate.match_percentage}", "Helvetica"),
                *([(candidate.match_rationale, "Helvetica")] if candidate.match_rationale else []),
                *([(f"Created date: {candidate.created_date}", "Helvetica")] if candidate.created_date else []),
                (f"Monday item ID: {candidate.monday_item_id}", "Helvetica")])
            write(
                f"{candidate.rank}. {candidate.project_title}",
                font_name="Helvetica-Bold",
            )
            write(f"Candidate TP Ref: {candidate.project_reference}")
            write(f"Title similarity: {candidate.match_percentage}")
            if candidate.match_rationale:
                write(candidate.match_rationale)
            if candidate.created_date:
                write(f"Created date: {candidate.created_date}")
            write(f"Monday item ID: {candidate.monday_item_id}", gap_after=8)

    action = (
        "Review the AI recommendation and matching evidence. Choose Accounts using the extracted Company Name "
        "as context, decide New Enq / Amend and, for an amendment, select the appropriate candidate TP Ref "
        "before manually moving the item to an active group."
    )
    reserve([("Reviewer action", "Helvetica-Bold"), (action, "Helvetica")])
    write("Reviewer action", font_name="Helvetica-Bold", gap_after=2)
    write(action)
    document.save()
    return buffer.getvalue()
