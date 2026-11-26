"""Contiguous page ranges of a long PDF for bounded transcription requests.

Ranges are complete original pages, never crops or reordered content, and are
only used after a whole-document request exceeds the provider deadline.
"""

import io
from math import ceil

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject


MAX_RANGE_PAGES = 10


def pdf_page_count(content: bytes) -> int:
    return len(PdfReader(io.BytesIO(content)).pages)


def pdf_page_ranges(content: bytes) -> list[tuple[int, int, bytes]]:
    """At least two ranges of at most MAX_RANGE_PAGES, as 1-based original page numbers."""
    reader = PdfReader(io.BytesIO(content))
    count = len(reader.pages)
    if count < 2:
        return []
    size = min(MAX_RANGE_PAGES, ceil(count / 2))
    ranges = []
    for start in range(0, count, size):
        end = min(start + size, count)
        with io.BytesIO() as output, PdfWriter() as writer:
            writer.append(reader, pages=(start, end), import_outline=False)
            # Preserve the original visibility configuration for layered drawings.
            if "/OCProperties" in reader.root_object:
                writer.root_object[NameObject("/OCProperties")] = reader.root_object["/OCProperties"].clone(writer)
            writer.write(output)
            ranges.append((start + 1, end, output.getvalue()))
    return ranges
