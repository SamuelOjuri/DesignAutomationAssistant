"""Bounded, overlapping views of large drawings for source transcription.

The original PDF always accompanies these views. Crops are not independent
sources and cannot establish a paper size. No PDF content is executed or stored.
"""

from copy import copy
import io

from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject


MAX_DETAIL_PAGES = 16
MAX_DETAIL_BYTES = 20 * 1024 * 1024
LARGE_PAGE_POINTS = 1400  # A2 and larger; ordinary A4 correspondence is unchanged.


def pdf_detail_views(content: bytes) -> tuple[bytes, list[str]]:
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF cannot supply detail views")
    writer, labels = PdfWriter(), []
    for index, page in enumerate(reader.pages):
        if max(page.cropbox.width, page.cropbox.height) < LARGE_PAGE_POINTS:
            continue
        if len(labels) + 4 > MAX_DETAIL_PAGES:
            break
        if page.rotation:
            page.transfer_rotation_to_content()
        left, bottom, right, top = map(float, page.cropbox)
        width, height = right - left, top - bottom
        # Overlap preserves labels and values near the centre lines.
        for name, bounds in (
            ("top left", (left, bottom + .45 * height, left + .55 * width, top)),
            ("top right", (left + .45 * width, bottom + .45 * height, right, top)),
            ("bottom left", (left, bottom, left + .55 * width, bottom + .55 * height)),
            ("bottom right", (left + .45 * width, bottom, right, bottom + .55 * height)),
        ):
            view = copy(page)
            view.mediabox = RectangleObject(bounds)
            view.cropbox = RectangleObject(bounds)
            writer.add_page(view)
            labels.append(f"Detail {len(labels) + 1}: original page {index + 1}, {name}")
    if not labels:
        return b"", []
    output = io.BytesIO()
    writer.write(output)
    if output.tell() > MAX_DETAIL_BYTES:
        return b"", []  # Full original is still read; only optional magnification is omitted.
    return output.getvalue(), labels
