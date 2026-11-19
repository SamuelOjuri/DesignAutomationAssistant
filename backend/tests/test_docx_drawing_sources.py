from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

from PIL import Image
import pytest

from backend.app.services.legacy_enquiry import email_extraction, source_documents
from backend.app.services.legacy_enquiry.email_extraction import extract_text_from_email
from backend.app.services.legacy_enquiry.source_documents import IncompleteSourceExtraction, extract_source_records
from backend.app.services.specification_image_text import literal_source_texts
from backend.app.services.specification_request_context import client_evidence_applies, source_parent_chain
from backend.app.services.specification_semantics import DOCX_EXPANDED_STATUS


_OOXML = "http://schemas.openxmlformats.org"
_TRANSCRIPTION = "[TEXT REGION 1]\nRoof 2 overview\n[/TEXT REGION 1]\nVISUAL CONTEXT\nIndicative drawing"


def _image():
    data = BytesIO()
    Image.new("RGB", (2, 2), "white").save(data, format="PNG")
    return data.getvalue()


def _drawing_docx(image, *, text="", target="media/drawing.png", part="document", external=False):
    data = BytesIO()
    ns = (f'xmlns:w="{_OOXML}/wordprocessingml/2006/main" xmlns:a="{_OOXML}/drawingml/2006/main" '
          f'xmlns:r="{_OOXML}/officeDocument/2006/relationships"')
    mode = ' TargetMode="External"' if external else ""
    with ZipFile(data, "w") as archive:
        archive.writestr(f"word/{part}.xml", f'<w:document {ns}><w:p><w:r><w:t>{text}</w:t>'
            '<w:drawing><a:blip r:embed="image"/><a:blip r:embed="image"/></w:drawing>'
            '</w:r></w:p></w:document>')
        archive.writestr(f"word/_rels/{part}.xml.rels",
            f'<Relationships xmlns="{_OOXML}/package/2006/relationships">\n'
            f'<Relationship Id="image" Type="{_OOXML}/officeDocument/2006/relationships/image" '
            f'Target="{target}"{mode}/></Relationships>')
        if image is not None:
            archive.writestr(f"word/{target}", image)
        archive.writestr("word/media/orphan.png", b"must never be sent to the model")
    return data.getvalue()


def test_embedded_word_drawing_is_literal_evidence_with_docx_and_email_provenance():
    image = _image()
    docx = _drawing_docx(image)
    bundle = BytesIO()
    with ZipFile(bundle, "w") as archive:
        archive.writestr("overview.docx", docx)

    calls = []

    def transcribe(content, filename):
        calls.append((content, filename))
        return _TRANSCRIPTION

    manifest = []
    context = extract_text_from_email(
        "Original request", [{"filename": "documents.zip", "content": bundle.getvalue()}],
        extractor=SimpleNamespace(process_image=transcribe), manifest=manifest,
    )
    body, archive, parent, drawing = manifest
    assert calls == [(image, "documents.zip!/overview.docx!/word/media/drawing.png")]
    assert parent["status"] == DOCX_EXPANDED_STATUS and parent["text"] == ""
    assert parent["contentSha256"] == sha256(docx).hexdigest()
    assert drawing["contentSha256"] == sha256(image).hexdigest()
    assert f"SOURCE ID: {drawing['sourceId']}\n" in context
    assert f"PARENT SOURCE ID: {parent['sourceId']}\n" in context
    sources = {source["sourceId"]: source for source in manifest}
    assert source_parent_chain(drawing, sources) == [parent, archive, body]
    assert literal_source_texts(drawing) == ["Roof 2 overview"]
    assert not client_evidence_applies({"request_role": "direct_request"}, drawing, sources, None, None)


def test_word_header_drawing_failure_holds_readable_paragraphs_for_retry():
    def fail(*args):
        raise TimeoutError("private URL")

    manifest = []
    docx = _drawing_docx(_image(), text="Roof 2 notes", part="header1")
    with pytest.raises(IncompleteSourceExtraction) as caught:
        extract_text_from_email("Original request", [{"filename": "overview.docx", "content": docx}],
            extractor=SimpleNamespace(process_image=fail), manifest=manifest)
    _, parent, drawing = manifest
    assert parent["status"] == "read" and parent["text"] == "Roof 2 notes"
    assert drawing["status"] == "failed:TimeoutError"
    assert drawing["parentSourceId"] == parent["sourceId"]
    assert "private URL" not in str(caught.value)


def test_external_word_images_are_not_fetched_and_text_is_preserved():
    docx = _drawing_docx(None, text="Roof 2 notes", target="https://example.invalid/drawing.png", external=True)
    source, = extract_source_records(docx, "overview.docx", None, source_id="asset:1")
    assert source["status"] == "read" and source["text"] == "Roof 2 notes"


@pytest.mark.parametrize(("limit", "expected"), [
    ("email", "email_attachment_limit"),
    ("archive_members", "archive_member_limit"),
    ("archive_bytes", "archive_expansion_limit"),
])
def test_word_drawings_consume_the_shared_email_and_archive_limits_before_transcription(monkeypatch, limit, expected):
    docx = _drawing_docx(_image())

    def transcribe(*args):
        pytest.fail("An over-budget drawing must not reach image transcription")

    client = SimpleNamespace(process_image=transcribe)
    if limit == "email":
        monkeypatch.setattr(email_extraction, "MAX_EMAIL_ATTACHMENTS", 1)
        with pytest.raises(IncompleteSourceExtraction) as caught:
            extract_text_from_email("Original request", [{"filename": "overview.docx", "content": docx}],
                                    extractor=client)
        records = caught.value.manifest
    else:
        if limit == "archive_members":
            monkeypatch.setattr(source_documents, "MAX_ARCHIVE_MEMBERS", 1)
        else:
            monkeypatch.setattr(source_documents, "MAX_ARCHIVE_EXPANDED_BYTES", len(docx))
        bundle = BytesIO()
        with ZipFile(bundle, "w") as archive:
            archive.writestr("overview.docx", docx)
        records = extract_source_records(bundle.getvalue(), "source.zip", client, source_id="asset:1")
    assert records[-1]["status"] == "failed:SourceDocumentError"
    assert records[-1]["error"]["code"] == expected


@pytest.mark.parametrize(("image", "target", "expected"), [
    (None, "media/missing.png", "invalid_docx_drawing"),
    (b"vector image", "media/drawing.emf", "unsupported_docx_drawing"),
])
def test_unreadable_embedded_word_drawing_has_explicit_diagnostics(image, target, expected):
    docx = _drawing_docx(image, target=target)
    records = extract_source_records(docx, "overview.docx", None, source_id="asset:1")
    assert records[-1]["status"] == "failed:SourceDocumentError"
    assert records[-1]["error"]["code"] == expected
