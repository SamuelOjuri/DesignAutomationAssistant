"""Synthetic PDF inputs verify view coverage and bounds without provider calls."""

import hashlib
import io
from types import SimpleNamespace

from google.genai import types
from httpx import ReadTimeout
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject
import pytest

from backend.app.config import settings
from backend.app.services import source_transcription_cache as cache
from backend.app.services.gemini_requests import GeminiRequestFailure
from backend.app.services.legacy_enquiry import pdf_detail_views as details
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.legacy_enquiry.source_documents import extract_source_records


def pdf(width=1684, height=2384, pages=1, rotation=0, protected=False):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width, height)
        page.mediabox = RectangleObject((10, 20, width + 10, height + 20))
        page.rotate(rotation)
    if protected:
        writer.encrypt("", "owner")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_detail_views_cover_rotated_original_with_overlap(rotation):
    content = pdf(rotation=rotation)
    original = PdfReader(io.BytesIO(content)).pages[0]
    if rotation:
        original.transfer_rotation_to_content()
    data, labels = details.pdf_detail_views(content)
    views = PdfReader(io.BytesIO(data)).pages
    assert len(views) == len(labels) == 4 and all(p.rotation == 0 for p in views)
    assert min(p.cropbox.left for p in views) == pytest.approx(original.cropbox.left)
    assert max(p.cropbox.right for p in views) == pytest.approx(original.cropbox.right)
    assert min(p.cropbox.bottom for p in views) == pytest.approx(original.cropbox.bottom)
    assert max(p.cropbox.top for p in views) == pytest.approx(original.cropbox.top)
    assert views[0].cropbox.right > views[1].cropbox.left
    assert views[0].cropbox.bottom < views[2].cropbox.top
    assert PdfReader(io.BytesIO(content)).pages[0].rotation == rotation  # Source bytes untouched.


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
