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


@dataclass(frozen=True, slots=True)
class MatchReport:
    source_item_id: str
    extracted_project_title: str
    extracted_company: str
    candidates: tuple[MatchReportCandidate, ...]

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
            )
            for candidate in match_contract.get("candidates", ())
        )
        return cls(
            source_item_id=str(source_item_id),
            extracted_project_title=str(
                match_contract.get("extractedProjectTitle") or "Not found"
            ),
            extracted_company=str(
                clean_extracted_value(extracted_company) or "Not found"
            ),
            candidates=candidates,
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
        required_height = len(lines) * leading + gap_after
        if y - required_height < bottom:
            new_page()
        document.setFont(font_name, font_size)
        for line in lines:
            document.drawString(left, y, line)
            y -= leading
        y -= gap_after

    write(
        "Matched Projects Review",
        font_name="Helvetica-Bold",
        font_size=18,
        leading=22,
        gap_after=8,
    )
    write(f"Source Monday item: {report.source_item_id}")
    write(f"Extracted project title: {report.extracted_project_title}")
    write(
        "Extracted Company (context only for the reviewer's Accounts decision; "
        f"not a resolved Monday account): {report.extracted_company}",
        gap_after=8,
    )
    write(
        f"Total potential matches: {len(report.candidates)}",
        font_name="Helvetica-Bold",
        gap_after=8,
    )

    if not report.candidates:
        write("No matches found.", font_name="Helvetica-Bold", gap_after=8)
    else:
        for candidate in report.candidates:
            write(
                f"{candidate.rank}. {candidate.project_title}",
                font_name="Helvetica-Bold",
            )
            write(f"Candidate TP Ref: {candidate.project_reference}")
            write(f"Match: {candidate.match_percentage}")
            if candidate.created_date:
                write(f"Created date: {candidate.created_date}")
            write(f"Monday item ID: {candidate.monday_item_id}", gap_after=8)

    write("Reviewer action", font_name="Helvetica-Bold", gap_after=2)
    write(
        "Choose Accounts using the extracted Company as context, decide New Enq / "
        "Amend, and, for an amendment, select the appropriate candidate TP Ref "
        "before moving the item to an active group."
    )
    document.save()
    return buffer.getvalue()
