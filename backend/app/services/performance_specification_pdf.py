"""Selectable-text PDF containing exactly the CAD handoff's 18 ordered fields."""

from functools import partial
import io
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from .performance_specification_contract import CADTranslation, FIELD_NAMES, SpecificationPublication, pdf_values


TITLE = "Project Performance Specification Details"


def render_performance_specification_pdf(
    translation: CADTranslation, *, item_id: str, revision: str,
) -> bytes:
    # ReportLab ships Vera: embedding it preserves common international names
    # and provides a ToUnicode map for the CAD text extractor.
    font_name = "PerformanceSpecificationVera"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(
            font_name, str(Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"),
        ))
    stream = io.BytesIO()
    document = SimpleDocTemplate(
        stream, pagesize=A4, leftMargin=40, rightMargin=40,
        topMargin=40, bottomMargin=42, title=TITLE, author="TaperedPlus",
    )
    heading = ParagraphStyle("SpecificationTitle", fontName=font_name, fontSize=17, leading=23,
                             textColor=colors.HexColor("#17324D"), spaceAfter=22)
    body = ParagraphStyle("SpecificationField", fontName=font_name, fontSize=10,
                          leading=15, spaceAfter=8, splitLongWords=True)
    review = ParagraphStyle("SpecificationReview", parent=body,
                            textColor=colors.HexColor("#744500"), backColor=colors.HexColor("#FFF4DC"),
                            borderPadding=6, spaceBefore=6, spaceAfter=12)
    values = pdf_values(translation)
    reviews = translation.field_reviews if isinstance(translation, SpecificationPublication) else {}
    story = [Paragraph(TITLE, heading)]
    if reviews:
        story.append(Paragraph("Fields marked Review required contain unresolved specifications. "
                               "The details are shown beside each affected field.", body))
        story.append(Spacer(1, 8))
    for label in FIELD_NAMES:
        story.append(Paragraph(f"{escape(label)}: {escape(values[label])}", review if label in reviews else body))
        story.append(Spacer(1, 2))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 7)
        canvas.setFillColor(colors.HexColor("#556575"))
        canvas.drawString(40, 23, f"Holding item {item_id} | Specification {revision[:16]}")
        canvas.drawRightString(A4[0] - 40, 23, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer,
                   canvasmaker=partial(Canvas, invariant=1))
    return stream.getvalue()
