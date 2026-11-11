"""Conservative native PDF text: ambiguous visual content keeps the OCR path.

Native text can be accurate while still omitting a scanned note, a form value or
outlined lettering. Accept only text-only pages with usable font mappings. In
particular, a digitally produced drawing is not automatically a text-only PDF.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from pypdf import PdfReader
from pypdf.generic import ContentStream


MAX_NATIVE_PAGES = 100
MAX_NATIVE_STREAM_BYTES = 2 * 1024 * 1024
MAX_NATIVE_DOCUMENT_STREAM_BYTES = 8 * 1024 * 1024
MAX_NATIVE_TEXT_CHARS = 500_000
_STANDARD_LATIN_FONTS = frozenset({
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
})
_VECTOR_PAINT = frozenset({b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh"})


@dataclass(frozen=True)
class NativePdfText:
    text: str | None
    reason: str


def _object(value):
    return value.get_object() if hasattr(value, "get_object") else value


def _usable_fonts(resources):
    fonts = _object(resources.get("/Font", {}))
    if not fonts:
        return False
    for reference in fonts.values():
        font = _object(reference)
        if font.get("/Subtype") == "/Type3":
            return False
        if font.get("/ToUnicode") is not None:
            continue
        base = str(font.get("/BaseFont", "")).lstrip("/")
        encoding = font.get("/Encoding")
        if (font.get("/Subtype") != "/Type1" or base not in _STANDARD_LATIN_FONTS
                or encoding is not None and str(encoding) not in {
                    "/WinAnsiEncoding", "/MacRomanEncoding", "/StandardEncoding",
                }):
            return False
    return True


def _readable_text(text):
    if len(text.strip()) < 80 or sum(character.isalpha() for character in text) < 40:
        return False
    # Replacement glyphs, private-use mappings and embedded controls are common
    # signs that the native font map is missing or unusable. Never repair digits.
    if any(character == "\ufffd" or unicodedata.category(character) in {"Co", "Cs", "Cn"}
           or unicodedata.category(character) == "Cc" and character not in "\n\r\t"
           for character in text):
        return False
    words = re.findall(r"[^\W\d_]{2,}", text, re.UNICODE)
    return len(words) >= 8 and all(len(word) <= 60 for word in words)


def native_pdf_text(reader: PdfReader) -> NativePdfText:
    """Return complete literal page text, or a bounded diagnostic for OCR fallback.

    No partial native text is returned on a rejected page. All pages still travel
    together through the existing per-document OCR/evidence contract.
    """
    try:
        if reader.is_encrypted:
            return NativePdfText(None, "encrypted_pdf")
        root = _object(reader.trailer["/Root"])
        form = _object(root.get("/AcroForm", {}))
        if form.get("/Fields") or form.get("/XFA"):
            return NativePdfText(None, "interactive_form")
        if root.get("/OCProperties"):
            return NativePdfText(None, "optional_content")
        if not 0 < len(reader.pages) <= MAX_NATIVE_PAGES:
            return NativePdfText(None, "page_limit")
        pages, total_stream_bytes, total_chars = [], 0, 0
        for index, page in enumerate(reader.pages, start=1):
            if page.get("/Annots"):
                return NativePdfText(None, "page_annotations")
            resources = _object(page.get("/Resources", {}))
            if resources.get("/XObject"):
                return NativePdfText(None, "image_or_form_xobject")
            if resources.get("/Pattern") or resources.get("/Shading") or resources.get("/ExtGState"):
                return NativePdfText(None, "complex_graphics")
            if not _usable_fonts(resources):
                return NativePdfText(None, "unusable_font_mapping")
            content = page.get_contents()
            if content is None:
                return NativePdfText(None, "missing_page_text")
            stream_bytes = len(content.get_data())
            total_stream_bytes += stream_bytes
            if stream_bytes > MAX_NATIVE_STREAM_BYTES or total_stream_bytes > MAX_NATIVE_DOCUMENT_STREAM_BYTES:
                return NativePdfText(None, "content_stream_limit")
            # Painted paths may be outlined letters absent from extract_text().
            # Even good native paragraphs cannot prove those labels were read.
            for operands, operator in ContentStream(content, reader).operations:
                if operator in _VECTOR_PAINT:
                    return NativePdfText(None, "painted_vector_graphics")
                if operator in {b"INLINE IMAGE", b"BI", b"Do"}:
                    return NativePdfText(None, "image_or_form_content")
                if operator in {b"W", b"W*"}:
                    return NativePdfText(None, "clipped_content")
                if operator == b"Tr" and (not operands or int(operands[0]) != 0):
                    return NativePdfText(None, "nonstandard_text_rendering")
                if ((operator == b"g" and list(operands) == [1])
                        or (operator == b"rg" and list(operands) == [1, 1, 1])
                        or (operator == b"k" and list(operands) == [0, 0, 0, 0])):
                    return NativePdfText(None, "white_text_or_graphics")
            # Plain mode includes all text orientations. Layout mode can silently
            # omit rotated labels and therefore cannot establish completeness.
            text = page.extract_text(extraction_mode="plain")
            if not isinstance(text, str) or not _readable_text(text):
                return NativePdfText(None, "insufficient_readable_text")
            total_chars += len(text)
            if total_chars > MAX_NATIVE_TEXT_CHARS:
                return NativePdfText(None, "native_text_limit")
            pages.append(f"PDF PAGE {index}\n{text.strip()}")
        return NativePdfText("\n\n".join(pages), "accepted")
    except Exception:
        # Native extraction is only an optimization. A malformed font or stream
        # must reach the established OCR/failure path rather than lose evidence.
        return NativePdfText(None, "native_parser_error")
