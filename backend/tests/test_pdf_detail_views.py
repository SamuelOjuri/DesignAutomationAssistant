"""Synthetic PDF inputs verify view coverage and bounds without provider calls."""

import hashlib
import io
from types import SimpleNamespace

from google.genai import types
from httpx import ReadTimeout
from pypdf import PdfReader, PdfWriter, apply_configuration
from pypdf.errors import LimitReachedError
from pypdf.generic import DecodedStreamObject, RectangleObject
import pytest

from backend.app.config import settings
from backend.app.services import source_transcription_cache as cache
from backend.app.services.gemini_requests import GeminiRequestFailure
from backend.app.services.legacy_enquiry import pdf_detail_views as details
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.legacy_enquiry.source_documents import extract_source_records


def pdf(width=1684, height=2384, pages=1, rotation=0, protected=False, line_segments=0):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width, height)
        page.mediabox = RectangleObject((10, 20, width + 10, height + 20))
        page.rotate(rotation)
        if line_segments:
            drawing = DecodedStreamObject()
            drawing.set_data(b"0 0 m 10 10 l S\n" * line_segments)
            page.replace_contents(drawing)
    if protected:
        writer.encrypt("", "owner")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


# User-space corners shown at the top left/right for each clockwise /Rotate.
DISPLAYED_TOP_CORNERS = {
    0: (("left", "top"), ("right", "top")),
    90: (("left", "bottom"), ("left", "top")),
    180: (("right", "bottom"), ("left", "bottom")),
    270: (("right", "top"), ("right", "bottom")),
}
OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}


def touches(box, original, corner):
    return all(getattr(box, edge) == pytest.approx(getattr(original, edge)) for edge in corner)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_detail_views_cover_rotated_original_with_overlap(rotation):
    content = pdf(rotation=rotation)
    original = PdfReader(io.BytesIO(content)).pages[0].cropbox
    data, labels = details.pdf_detail_views(content)
    views = PdfReader(io.BytesIO(data)).pages
    # Views keep /Rotate, so crops stay in the original's user space.
    assert len(views) == len(labels) == 4 and all(p.rotation == rotation for p in views)
    boxes = [p.cropbox for p in views]
    assert min(b.left for b in boxes) == pytest.approx(original.left)
    assert max(b.right for b in boxes) == pytest.approx(original.right)
    assert min(b.bottom for b in boxes) == pytest.approx(original.bottom)
    assert max(b.top for b in boxes) == pytest.approx(original.top)
    # 55% per axis gives every neighbouring pair a 10% overlap.
    assert all(b.width == pytest.approx(.55 * original.width)
               and b.height == pytest.approx(.55 * original.height) for b in boxes)
    top_left, top_right = DISPLAYED_TOP_CORNERS[rotation]
    for box, label, corner in zip(boxes, labels, (
        top_left, top_right,
        tuple(OPPOSITE[edge] for edge in top_right), tuple(OPPOSITE[edge] for edge in top_left),
    )):
        assert touches(box, original, corner), label
    assert [label.rsplit(", ", 1)[1] for label in labels] == ["top left", "top right", "bottom left", "bottom right"]
    assert PdfReader(io.BytesIO(content)).pages[0].rotation == rotation  # Source bytes untouched.


def test_dense_rotated_drawing_keeps_detail_views_without_parsing_its_content():
    # Render job d3e6a8a9 (item 3377374422): two A0 drawings with /Rotate 90 and
    # 13-18 MB content streams exceeded pypdf's per-stream token limit when the
    # rotation was moved into their content, failing both sources on all attempts.
    content = pdf(2384, 3370, rotation=90, line_segments=100)
    with apply_configuration(stream_parsing_work_maximum_count=50):
        data, labels = details.pdf_detail_views(content)
    original = PdfReader(io.BytesIO(content)).pages[0].get_contents().get_data()
    views = PdfReader(io.BytesIO(data)).pages
    assert len(views) == len(labels) == 4
    assert all(view.get_contents().get_data() == original for view in views)


def test_detail_view_parser_failure_still_transcribes_the_original(monkeypatch, caplog):
    class LimitedWriter(PdfWriter):
        def write(self, stream):
            raise LimitReachedError("Reached maximum number of tokens per stream.")

    monkeypatch.setattr(details, "PdfWriter", LimitedWriter)
    calls = []

    def generate(model, parts, config):
        calls.append(parts)
        return SimpleNamespace(text="SCALE 1:100")

    content = pdf()
    assert LegacyGeminiClient("offline", generate_content=generate).process_pdf(content, "drawing.pdf") == "SCALE 1:100"
    assert len(calls) == 1 and len(calls[0]) == 2  # Original, then its transcription prompt.
    assert calls[0][0].inline_data.data == content
    assert "PDF detail views omitted error_type=LimitReachedError" in caplog.text


def test_ordinary_pages_have_no_detail_pdf_and_large_documents_are_bounded(monkeypatch):
    assert details.pdf_detail_views(pdf(595, 842)) == (b"", [])
    data, labels = details.pdf_detail_views(pdf(pages=8))
    assert len(PdfReader(io.BytesIO(data)).pages) == len(labels) == details.MAX_DETAIL_PAGES
    monkeypatch.setattr(details, "MAX_DETAIL_BYTES", 1)
    assert details.pdf_detail_views(pdf()) == (b"", [])


def test_pdf_request_keeps_original_and_labels_detail_pages():
    calls = []
    def generate(model, parts, config):
        calls.append((parts, config))
        return SimpleNamespace(text="SCALE 1:100")
    content = pdf()
    assert LegacyGeminiClient("offline", generate_content=generate).process_pdf(content, "drawing.pdf") == "SCALE 1:100"
    parts, config = calls[0]
    assert parts[0].inline_data.data == content
    assert len(PdfReader(io.BytesIO(parts[2].inline_data.data)).pages) == 4
    assert "original page 1, bottom right" in parts[1]
    assert config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH


def test_detail_read_timeout_retries_the_full_original_outside_the_detail_cache(monkeypatch):
    saved = {}
    monkeypatch.setattr(settings, "design_processing_transcription_cache_enabled", True)
    monkeypatch.setattr(cache, "read_text", lambda scope, key: saved.get((scope, key)))
    monkeypatch.setattr(cache, "save_text", lambda scope, key, digest, text: saved.update({(scope, key): text}))
    calls = []

    def generate(model, parts, config):
        calls.append((parts, config))
        if len(calls) == 1:
            raise ReadTimeout("private provider detail")
        return SimpleNamespace(text="Recovered original" if len(calls) == 2 else "Detail transcription")

    content = pdf(pages=2)
    client = LegacyGeminiClient("offline", generate_content=generate, pdf_native_text_enabled=False,
                                transcription_cache_scope="test-pdf")

    record, = extract_source_records(content, "drawing.pdf", client, source_id="asset:test")
    assert record["status"] == "read"
    assert record["text"] == "Recovered original"
    assert record["contentSha256"] == hashlib.sha256(content).hexdigest()
    assert not saved
    original_only, config = calls[1]
    assert len(original_only) == 2  # Full PDF, then its transcription prompt.
    assert original_only[0].inline_data.data == content
    assert config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH
    assert client.process_pdf(content, "drawing.pdf") == "Detail transcription"
    assert client.process_pdf(content, "drawing.pdf") == "Detail transcription"
    assert len(calls) == 3


@pytest.mark.parametrize("error", [ReadTimeout, ValueError])
def test_no_detail_retry_for_single_original_timeout_or_unrelated_failure(error):
    calls = []

    def fail(model, parts, config):
        calls.append(parts)
        raise error("private provider detail")

    content = pdf(595, 842) if error is ReadTimeout else pdf()
    with pytest.raises(GeminiRequestFailure) as failure:
        LegacyGeminiClient("offline", generate_content=fail).process_pdf(content, "drawing.pdf")

    assert len(calls) == 1
    assert failure.value.details["error_type"] == error.__name__


def test_detail_timeout_fallback_rejects_truncated_source_text():
    calls = []

    def generate(model, parts, config):
        calls.append(parts)
        if len(calls) == 1:
            raise ReadTimeout("private provider detail")
        return SimpleNamespace(text="Partial text", candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")])

    client = LegacyGeminiClient("offline", generate_content=generate, pdf_native_text_enabled=False)
    records = extract_source_records(pdf(), "drawing.pdf", client, source_id="asset:test")

    assert len(calls) == 2
    assert records[0]["status"] == "failed:GeminiRequestFailure"
    assert records[0]["error"]["code"] == "incomplete_response"
    assert not records[0]["text"]


def test_encrypted_pdf_never_reaches_provider_even_when_called_directly():
    def unexpected(*args):
        pytest.fail("Protected source was sent to provider")
    with pytest.raises(ValueError, match="Encrypted PDF"):
        LegacyGeminiClient("offline", generate_content=unexpected).process_pdf(pdf(protected=True), "protected.pdf")


class ProviderDeadline(Exception):
    code = 504
    status = "DEADLINE_EXCEEDED"


def cached_client(monkeypatch, generate):
    saved = {}
    monkeypatch.setattr(settings, "design_processing_transcription_cache_enabled", True)
    monkeypatch.setattr(cache, "read_text", lambda scope, key: saved.get((scope, key)))
    monkeypatch.setattr(cache, "save_text", lambda scope, key, digest, text: saved.update({(scope, key): text}))
    return saved, LegacyGeminiClient("offline", generate_content=generate, pdf_native_text_enabled=False,
                                     transcription_cache_scope="test-pdf")


def test_provider_deadline_transcribes_page_ranges_with_original_page_numbers(monkeypatch):
    # Render job 67b6abf3 (item 3386619146): a 48-page A4 PDF returned HTTP 504
    # DEADLINE_EXCEEDED on every attempt, failing the item after three attempts.
    calls = []

    def generate(model, parts, config):
        calls.append((parts, config))
        if len(calls) == 1:
            raise ProviderDeadline("private provider detail")
        return SimpleNamespace(text=f"Range {len(calls) - 1}")

    saved, client = cached_client(monkeypatch, generate)
    content = pdf(595, 842, pages=23)
    record, = extract_source_records(content, "House 1.pdf", client, source_id="asset:test")

    assert record["status"] == "read" and record["text"] == "Range 1\n\nRange 2\n\nRange 3"
    assert record["contentSha256"] == hashlib.sha256(content).hexdigest()
    ranges = calls[1:]
    assert [len(PdfReader(io.BytesIO(parts[0].inline_data.data)).pages) for parts, _ in ranges] == [10, 10, 3]
    assert [parts[1].split(" of ")[0] for parts, _ in ranges] == [
        f"This PDF contains only pages {pages}" for pages in ("1-10", "11-20", "21-23")]
    assert "Its first page is original page 11;" in ranges[1][0][1]
    assert all(config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH for _, config in ranges)
    assert list(saved.values()) == [record["text"]]  # Only the complete page-range text is cached.
    assert client.process_pdf(content, "House 1.pdf") == record["text"]
    assert len(calls) == 4  # Reuse skips the whole-document request that exceeded the deadline.


def test_single_page_deadline_is_not_split():
    calls = []

    def deadline(model, parts, config):
        calls.append(parts)
        raise ProviderDeadline("private provider detail")

    with pytest.raises(GeminiRequestFailure) as failure:
        LegacyGeminiClient("offline", generate_content=deadline).process_pdf(pdf(595, 842), "drawing.pdf")
    assert len(calls) == 1
    assert (failure.value.http_status, failure.value.details["provider_status"]) == (504, "DEADLINE_EXCEEDED")


def test_failed_page_range_fails_the_source_without_caching_partial_text(monkeypatch):
    calls = []

    def generate(model, parts, config):
        calls.append(parts)
        if len(calls) in (1, 3):
            raise ProviderDeadline("private provider detail")
        return SimpleNamespace(text="Range text")

    saved, client = cached_client(monkeypatch, generate)
    record, = extract_source_records(pdf(595, 842, pages=12), "House 1.pdf", client, source_id="asset:test")

    assert len(calls) == 3  # Whole document, then two six-page ranges.
    assert record["status"] == "failed:GeminiRequestFailure" and record["error"]["http_status"] == 504
    assert not record["text"] and not saved
