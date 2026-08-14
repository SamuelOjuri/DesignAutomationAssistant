from __future__ import annotations

import io
from typing import Any, Mapping, Optional

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .legacy_enquiry.formatting import build_ai_data_rows


_LEFT_MARGIN = 36
_RIGHT_MARGIN = 36
_TOP_MARGIN = 36
_BOTTOM_MARGIN = 36
_CELL_PADDING = 5
_BODY_FONT = "Helvetica"
_BOLD_FONT = "Helvetica-Bold"
_BODY_FONT_SIZE = 8.5
_LEADING = 11
_COLUMN_WIDTHS = (136, 297, 90)


def _wrapped_lines(text: Any, max_width: float) -> list[str]:
    words = str(text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if stringWidth(candidate, _BODY_FONT, _BODY_FONT_SIZE) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        while stringWidth(word, _BODY_FONT, _BODY_FONT_SIZE) > max_width:
            split_at = len(word)
            while (
                split_at > 1
                and stringWidth(word[:split_at], _BODY_FONT, _BODY_FONT_SIZE)
                > max_width
            ):
                split_at -= 1
            lines.append(word[:split_at])
            word = word[split_at:]
        current = word
    if current or not lines:
        lines.append(current)
    return lines


def render_ai_data_pdf(
    parameters: Mapping[str, Any],
    sources: Optional[Mapping[str, Any]] = None,
) -> bytes:
    rows = build_ai_data_rows(parameters, sources)
    buffer = io.BytesIO()
    page_width, page_height = A4
    document = canvas.Canvas(
        buffer,
        pagesize=A4,
        pageCompression=0,
        invariant=1,
    )
    table_width = sum(_COLUMN_WIDTHS)
    y = page_height - _TOP_MARGIN

    def draw_title() -> None:
        nonlocal y
        document.setFillColor(HexColor("#17324D"))
        document.setFont(_BOLD_FONT, 18)
        document.drawString(_LEFT_MARGIN, y, "AI Data Preview")
        y -= 28

    def draw_header() -> None:
        nonlocal y
        height = 22
        document.setFillColor(HexColor("#17324D"))
        document.rect(_LEFT_MARGIN, y - height, table_width, height, fill=1, stroke=0)
        document.setFillColor(white)
        document.setFont(_BOLD_FONT, 9)
        x = _LEFT_MARGIN
        for label, width in zip(("Parameter", "Value", "Source"), _COLUMN_WIDTHS):
            document.drawString(x + _CELL_PADDING, y - 15, label)
            x += width
        y -= height

    def new_page() -> None:
        nonlocal y
        document.showPage()
        y = page_height - _TOP_MARGIN
        draw_title()
        draw_header()

    draw_title()
    draw_header()
    for row_index, row in enumerate(rows):
        wrapped = [
            _wrapped_lines(value, width - 2 * _CELL_PADDING)
            for value, width in zip(row, _COLUMN_WIDTHS)
        ]
        row_height = max(len(lines) for lines in wrapped) * _LEADING + 2 * _CELL_PADDING
        if y - row_height < _BOTTOM_MARGIN:
            new_page()
        fill = HexColor("#F3F6F8") if row_index % 2 == 0 else white
        document.setFillColor(fill)
        document.setStrokeColor(HexColor("#C7D1D9"))
        document.rect(
            _LEFT_MARGIN,
            y - row_height,
            table_width,
            row_height,
            fill=1,
            stroke=1,
        )
        document.setFillColor(HexColor("#17212B"))
        document.setFont(_BODY_FONT, _BODY_FONT_SIZE)
        x = _LEFT_MARGIN
        for lines, width in zip(wrapped, _COLUMN_WIDTHS):
            text_y = y - _CELL_PADDING - _BODY_FONT_SIZE
            for line in lines:
                document.drawString(x + _CELL_PADDING, text_y, line)
                text_y -= _LEADING
            x += width
            document.line(x, y, x, y - row_height)
        y -= row_height

    document.save()
    return buffer.getvalue()