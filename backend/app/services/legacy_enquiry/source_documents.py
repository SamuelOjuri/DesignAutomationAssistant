"""Source-labelled, bounded attachment extraction shared with task ingestion.

Every readable source has its own text and hash. Failures are not negative
evidence: callers retain their previous successful output and retry extraction.
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import io
from pathlib import Path
import tempfile
from defusedxml import ElementTree
from zipfile import ZipFile

from ..spreadsheet_extraction import extract_spreadsheet_documents, is_spreadsheet


MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_CONTEXT_CHARS = 500_000
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


class IncompleteSourceExtraction(ValueError):
    def __init__(self, manifest):
        self.manifest = manifest
        failures = [f"{source['filename']}: {source['status']}" for source in manifest if source["status"] != "read"]
        super().__init__("Incomplete source extraction; retry/review required: " + "; ".join(failures)[:1000])


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "tr", "blockquote", "hr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "tr", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(content: str | bytes) -> str:
    parser = _HTMLText()
    parser.feed(content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content)
    return "".join(parser.parts).strip()


def extract_document(content: bytes, filename: str, extractor) -> str:
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Source exceeds the extraction size limit")
    suffix = Path(filename).suffix.lower()
    if is_spreadsheet(filename):
        with tempfile.TemporaryDirectory(prefix="design-sheet-") as directory:
            path = Path(directory) / ("source" + suffix)
            path.write_bytes(content)
            documents = extract_spreadsheet_documents(str(path), filename, visible_only=True)
        if any(doc["section"] == "spreadsheet:extraction-notice" for doc in documents):
            raise ValueError("Workbook extraction incomplete (limits, unreadable cells, or uncached formulas)")
        return "\n\n".join(doc["text"] for doc in documents)
    if suffix == ".pdf":
        # Per-document calls retain identity and make individual failures retryable.
        text = extractor.process_pdf(content, filename)
    elif suffix in IMAGE_EXTENSIONS:
        if suffix in {".gif", ".bmp"}:
            from PIL import Image, ImageSequence
            with Image.open(io.BytesIO(content)) as image:
                # Do not silently ignore later frames of an animated document.
                if getattr(image, "n_frames", 1) > 1:
                    raise ValueError("Multi-frame image requires conversion/review")
                output = io.BytesIO()
                next(ImageSequence.Iterator(image)).convert("RGB").save(output, format="PNG")
                content, filename = output.getvalue(), filename + ".png"
        text = extractor.process_image(content, filename)
    elif suffix == ".docx":
        with ZipFile(io.BytesIO(content)) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > 100 * 1024 * 1024:
                raise ValueError("Document exceeds the decompression limit")
            text = "\n".join(
                "".join(node.itertext())
                for name in archive.namelist()
                if name == "word/document.xml" or name.startswith(("word/header", "word/footer")) and name.endswith(".xml")
                for node in ElementTree.fromstring(archive.read(name)).iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")
            )
    elif suffix in {".txt", ".csv", ".tsv", ".json", ".xml"}:
        text = content.decode("utf-8-sig")
    elif suffix in {".html", ".htm"}:
        text = html_text(content)
    else:
        raise ValueError("Unsupported source format; provide a readable export")
    if not isinstance(text, str) or not text.strip() or text.lstrip().lower().startswith((
        "error processing", "unable to process", "unsupported image format",
    )):
        raise ValueError("Document extraction returned no readable text")
    if len(text) > MAX_CONTEXT_CHARS:
        raise ValueError("Source exceeds the extracted text limit")
    return text


def source_record(source_id, filename, content, *, source_type, observed_at=None, text="", status="read"):
    return {
        "sourceId": source_id, "filename": filename, "sourceType": source_type,
        "contentSha256": hashlib.sha256(content).hexdigest(), "sizeBytes": len(content),
        "observedAt": observed_at, "status": status, "text": text,
    }
