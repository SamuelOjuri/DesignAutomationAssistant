"""Native text must be complete enough to replace, rather than supplement, OCR."""

import io
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from backend.app.services.legacy_enquiry.native_pdf_text import native_pdf_text
from backend.app.services.legacy_enquiry.source_documents import extract_source_records


TITLE_LINES = (
    "Contract Name: St Clere's School",
    "Contract No: B260681",
    "Drawing No: LTF-001; Ref No: D.S - 00074_26.01",
    "Scale: 1:500 @ ISO A3",
    "Material: BauderPIR FA G16; Deck Type: Bauder - Concrete",
    "Please revise NB1 Roof B and NB2 Roof B with a 40 mm sump depth.",
)


def text_pdf(*, pages=1, image=False, vector=False, hidden=False, blank_last=False):
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(842, 595), pageCompression=0)
    for page_number in range(pages):
        if not (blank_last and page_number == pages - 1):
            text = document.beginText(30, 560)
            if hidden:
                text.setTextRenderMode(3)
            for line in TITLE_LINES:
                text.textLine(line)
            document.drawText(text)
        if image:
            document.drawImage(ImageReader(Image.new("RGB", (8, 8), "black")), 20, 20, width=100, height=60)
        if vector:
            document.rect(20, 20, 100, 60, fill=1)
        document.showPage()
    document.save()
    return output.getvalue()


def client(*, enabled=True):
    calls = []
    return SimpleNamespace(pdf_native_text_enabled=enabled,
        process_pdf=lambda *args: calls.append(args) or "OCR preserved all visible content"), calls


def test_text_only_pdf_preserves_literal_title_values_and_page_boundaries_without_ocr():
    extractor, calls = client()
    record, = extract_source_records(text_pdf(pages=2), "digital-specification.pdf", extractor, source_id="pdf")
    assert record["status"] == "read" and record["pdfTextMethod"] == "native"
    assert not calls
    for literal in ("B260681", "D.S - 00074_26.01", "1:500 @ ISO A3", "BauderPIR FA G16"):
        assert record["text"].count(literal) == 2
    assert record["text"].index("PDF PAGE 1") < record["text"].index("PDF PAGE 2")
    assert "pdfNativeTextFallbackReason" not in record


@pytest.mark.parametrize("options,reason", [
    ({"image": True}, "image_or_form_xobject"),
    ({"image": True, "blank_last": True}, "image_or_form_xobject"),
    ({"vector": True}, "painted_vector_graphics"),
    ({"hidden": True}, "nonstandard_text_rendering"),
    ({"pages": 2, "blank_last": True}, "insufficient_readable_text"),
])
def test_mixed_visual_content_or_incomplete_native_pages_fall_back_as_a_whole(options, reason):
    extractor, calls = client()
    record, = extract_source_records(text_pdf(**options), "mixed.pdf", extractor, source_id="pdf")
    assert record["status"] == "read"
    assert record["text"] == "OCR preserved all visible content"
    assert record["pdfTextMethod"] == "gemini"
    assert record["pdfNativeTextFallbackReason"] == reason
    assert len(calls) == 1


def test_form_values_never_disappear_behind_a_good_native_text_layer():
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(text_pdf())))
    field = DictionaryObject({NameObject("/FT"): NameObject("/Tx"),
        NameObject("/T"): TextStringObject("contract"), NameObject("/V"): TextStringObject("DIFFERENT CONTRACT")})
    writer.root_object[NameObject("/AcroForm")] = DictionaryObject({
        NameObject("/Fields"): ArrayObject([writer._add_object(field)]),
    })
    output = io.BytesIO()
    writer.write(output)
    extractor, calls = client()
    record, = extract_source_records(output.getvalue(), "filled-form.pdf", extractor, source_id="pdf")
    assert record["pdfNativeTextFallbackReason"] == "interactive_form"
    assert len(calls) == 1


def test_sticky_note_annotations_are_not_silently_omitted():
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(text_pdf())))
    writer.pages[0][NameObject("/Annots")] = ArrayObject([DictionaryObject({
        NameObject("/Subtype"): NameObject("/Text"), NameObject("/Contents"): TextStringObject("Updated sump: 40mm"),
    })])
    output = io.BytesIO()
    writer.write(output)
    extractor, calls = client()
    record, = extract_source_records(output.getvalue(), "annotated.pdf", extractor, source_id="pdf")
    assert record["pdfNativeTextFallbackReason"] == "page_annotations"
    assert len(calls) == 1


def test_native_fast_path_can_be_disabled_and_fake_clients_keep_the_existing_contract():
    extractor, calls = client(enabled=False)
    record, = extract_source_records(text_pdf(), "digital.pdf", extractor, source_id="pdf")
    assert len(calls) == 1 and record["text"] == "OCR preserved all visible content"
    assert "pdfTextMethod" not in record
    del extractor.pdf_native_text_enabled
    extract_source_records(text_pdf(), "digital.pdf", extractor, source_id="pdf")
    assert len(calls) == 2


def test_native_parser_error_falls_back_instead_of_becoming_a_new_source_failure(monkeypatch):
    reader = PdfReader(io.BytesIO(text_pdf()))
    monkeypatch.setattr(reader.pages[0], "extract_text", lambda **kwargs: (_ for _ in ()).throw(ValueError("bad font")))
    result = native_pdf_text(reader)
    assert result.text is None and result.reason == "native_parser_error"


def test_missing_font_mapping_falls_back_even_when_ascii_text_would_look_plausible():
    reader = PdfReader(io.BytesIO(text_pdf()))
    for reference in reader.pages[0]["/Resources"]["/Font"].values():
        reference.get_object()[NameObject("/BaseFont")] = NameObject("/CustomUnmapped")
    result = native_pdf_text(reader)
    assert result.text is None and result.reason == "unusable_font_mapping"


def test_garbled_native_text_falls_back_without_repairing_or_inventing_characters(monkeypatch):
    reader = PdfReader(io.BytesIO(text_pdf()))
    monkeypatch.setattr(reader.pages[0], "extract_text", lambda **kwargs: "\n".join(TITLE_LINES) + "\ufffd")
    result = native_pdf_text(reader)
    assert result.text is None and result.reason == "insufficient_readable_text"


def test_native_text_inside_form_xobjects_requires_the_existing_visual_read():
    output = io.BytesIO()
    document = canvas.Canvas(output, pageCompression=0)
    for index, line in enumerate(TITLE_LINES):
        document.drawString(30, 560 - index * 15, line)
    document.beginForm("supplementary-title")
    document.drawString(30, 300, "Contract No: REVISED B260681")
    document.endForm()
    document.doForm("supplementary-title")
    document.save()
    extractor, calls = client()
    record, = extract_source_records(output.getvalue(), "form-xobject.pdf", extractor, source_id="pdf")
    assert record["pdfNativeTextFallbackReason"] == "image_or_form_xobject"
    assert len(calls) == 1
