"""Bounded, overlapping views of large drawings for source transcription.

The original PDF always accompanies these views. Crops are not independent
sources and cannot establish a paper size. No PDF content is executed or stored.
"""

from copy import copy
import io
import logging

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.errors import PyPdfError
from pypdf.generic import RectangleObject


logger = logging.getLogger(__name__)
MAX_DETAIL_PAGES = 16
MAX_DETAIL_BYTES = 20 * 1024 * 1024
LARGE_PAGE_POINTS = 1400  # A2 and larger; ordinary A4 correspondence is unchanged.


def _bounds(transformation, left, bottom, right, top):
    corners = [transformation.apply_on((x, y)) for x in (left, right) for y in (bottom, top)]
    xs, ys = [x for x, _ in corners], [y for _, y in corners]
    return tuple(round(value, 3) for value in (min(xs), min(ys), max(xs), max(ys)))


def pdf_detail_views(content: bytes) -> tuple[bytes, list[str]]:
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF cannot supply detail views")
    try:
        return _detail_views(reader)
    except PyPdfError as exc:
        # Magnification is optional; the full original is still transcribed.
        logger.warning("PDF detail views omitted error_type=%s", type(exc).__name__)
        return b"", []


def _detail_views(reader):
    writer, labels = PdfWriter(), []
    for index, page in enumerate(reader.pages):
        if max(page.cropbox.width, page.cropbox.height) < LARGE_PAGE_POINTS:
            continue
        if len(labels) + 4 > MAX_DETAIL_PAGES:
            break
        # Quadrants are named as displayed but cropped in user space with /Rotate
        # kept. Moving rotation into the content would parse every drawing operator;
        # dense CAD drawings (item 3377374422) exceed pypdf's per-stream token limit.
        to_display = Transformation().rotate(-page.rotation)
        to_user = Transformation().rotate(page.rotation)
        left, bottom, right, top = _bounds(to_display, *map(float, page.cropbox))
        width, height = right - left, top - bottom
        # Overlap preserves labels and values near the centre lines.
        for name, bounds in (
            ("top left", (left, bottom + .45 * height, left + .55 * width, top)),
            ("top right", (left + .45 * width, bottom + .45 * height, right, top)),
            ("bottom left", (left, bottom, left + .55 * width, bottom + .55 * height)),
            ("bottom right", (left + .45 * width, bottom, right, bottom + .55 * height)),
        ):
            view = copy(page)
            box = _bounds(to_user, *bounds)
            view.mediabox = RectangleObject(box)
            view.cropbox = RectangleObject(box)
            writer.add_page(view)
            labels.append(f"Detail {len(labels) + 1}: original page {index + 1}, {name}")
    if not labels:
        return b"", []
    output = io.BytesIO()
    writer.write(output)
    if output.tell() > MAX_DETAIL_BYTES:
        return b"", []  # Full original is still read; only optional magnification is omitted.
    return output.getvalue(), labels
