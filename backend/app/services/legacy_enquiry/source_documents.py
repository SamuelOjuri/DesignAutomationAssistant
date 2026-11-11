"""Source-labelled, bounded attachment extraction shared with task ingestion.

Every readable source has its own text and hash. Failures are not negative
evidence: callers retain their previous successful output and retry extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import io
from pathlib import Path
import tempfile
from zipfile import BadZipFile, ZipFile
import zlib

from defusedxml import ElementTree
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from ..gemini_requests import GeminiRequestFailure, source_failure_details as gemini_failure_details
from ..spreadsheet_extraction import extract_spreadsheet_documents, is_spreadsheet
from ..specification_semantics import (
    ANIMATED_IMAGE_SKIP_STATUS, ARCHIVE_EXPANDED_STATUS, COMPLETE_SOURCE_STATUSES, DWG_SKIP_STATUS,
    ENCRYPTED_PDF_SKIP_STATUS, PROVIDER_RECITATION_REVIEW_STATUS,
)
from ..specification_image_text import literal_image_regions
from ..source_text import clean_source_text


MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200
MAX_ARCHIVE_DEPTH = 3
MAX_CONTEXT_CHARS = 500_000
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


class AnimatedImageSkipped(Exception):
    """Animated images are excluded from extraction by the configured business rule."""


class DwgSkipped(Exception):
    """DWG files are audited but intentionally excluded from design extraction."""


class EncryptedPdfSkipped(Exception):
    """Encrypted PDFs are audited but intentionally excluded from extraction."""


class SourceDocumentError(ValueError):
    """Actionable, application-owned diagnostics safe to persist in a manifest."""

    def __init__(self, code, message):
        self.details = {"stage": "source_document", "code": code, "message": message}
        super().__init__(message)


class UnsupportedSourceFormat(SourceDocumentError):
    def __init__(self):
        super().__init__("unsupported_source_format", "Unsupported source format; replace this extraction source with a readable export.")


def source_failure_details(exc):
    if isinstance(exc, SourceDocumentError):
        return exc.details
    # Arbitrary parser/provider messages can contain source text or signed URLs.
    return gemini_failure_details(exc)


def _provider_recitation(exc):
    # Recitation recurs for unchanged bytes, so retrying only repeats the block.
    # Safety/prompt blocks, truncation and empty output remain retryable failures.
    details = exc.details if isinstance(exc, GeminiRequestFailure) else {}
    return (details.get("stage") in {"image_text", "pdf_text"} and details.get("code") == "blocked_response"
            and details.get("finish_reason") == "RECITATION"
            and details.get("block_reason") in {None, "BLOCK_REASON_UNSPECIFIED"})


def _failure_summary(source):
    summary = f"{source['filename']}: {source['status']}"
    error = source.get("error")
    if isinstance(error, dict):
        if error.get("stage") == "source_document":
            summary += f" ({error['message']})"
        else:
            summary += f" (stage={error.get('stage')}, http_status={error.get('http_status')}, mode={error.get('mode')})"
            if error.get("code"):
                summary += f" (code={error['code']}, finish_reason={error.get('finish_reason')})"
    return summary


class IncompleteSourceExtraction(ValueError):
    def __init__(self, manifest):
        self.manifest = manifest
        failures = [_failure_summary(source) for source in manifest
                    if source["status"] not in COMPLETE_SOURCE_STATUSES]
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
        if tag == "li":
            self.parts.append("\n* ")
        if tag in {"td", "th"}:
            self.parts.append(" ")
        if tag == "img" and not self.hidden:
            source = dict(attrs).get("src") or ""
            if source.lower().startswith("cid:"):
                self.parts.append(f" [Image: {source}] ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "tr", "blockquote", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(content: str | bytes) -> str:
    parser = _HTMLText()
    parser.feed(content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content)
    return clean_source_text("".join(parser.parts).strip())


def extract_document(content: bytes, filename: str, extractor) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".dwg":
        raise DwgSkipped("DWG drawings are excluded from extraction")
    if len(content) > MAX_SOURCE_BYTES:
        raise SourceDocumentError("source_size_limit", "Source exceeds the 20 MiB extraction size limit; supply a smaller document.")
    if is_spreadsheet(filename):
        with tempfile.TemporaryDirectory(prefix="design-sheet-") as directory:
            path = Path(directory) / ("source" + suffix)
            path.write_bytes(content)
            documents = extract_spreadsheet_documents(str(path), filename, visible_only=True, preserve_full_cells=True)
        notices = [doc["notice"] for doc in documents if doc["section"] == "spreadsheet:extraction-notice"]
        if notices:
            raise SourceDocumentError("workbook_incomplete", "Workbook extraction incomplete: " + " ".join(notices)
                                      + " Recalculate and save uncached formulas in Excel, or supply a smaller readable export.")
        text = "\n\n".join(doc["text"] for doc in documents)
    elif suffix == ".pdf":
        try:
            with PdfReader(io.BytesIO(content)) as reader:
                # Skip all encryption, including owner-only restrictions with
                # an empty opening password. Never send these bytes to Gemini.
                if reader.is_encrypted:
                    raise EncryptedPdfSkipped("Encrypted PDFs are excluded from extraction")
        except PdfReadError:
            raise SourceDocumentError("unreadable_pdf", "PDF could not be read; supply a valid PDF export.") from None
        # Per-document calls retain identity and make individual failures retryable.
        text = extractor.process_pdf(content, filename)
    elif suffix in IMAGE_EXTENSIONS:
        from PIL import Image
        with Image.open(io.BytesIO(content)) as image:
            # Inspect metadata only. No frame is sent to the model for animations,
            # including GIF, APNG and animated WebP.
            if getattr(image, "is_animated", False):
                raise AnimatedImageSkipped("Animated images are excluded from extraction")
            if suffix in {".gif", ".bmp"}:
                output = io.BytesIO()
                image.convert("RGB").save(output, format="PNG")
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
        raise UnsupportedSourceFormat()
    if not isinstance(text, str) or not text.strip() or text.lstrip().lower().startswith((
        "error processing", "unable to process", "unsupported image format",
    )):
        raise SourceDocumentError("no_readable_text", "Document extraction returned no readable text; review the source or retry transcription.")
    if len(text) > MAX_CONTEXT_CHARS:
        raise SourceDocumentError("source_text_limit", "Source exceeds the extracted text limit; split the source document.")
    return clean_source_text(text)


@dataclass
class _ArchiveBudget:
    members: int = 0
    expanded_bytes: int = 0
    text_chars: int = 0


def extract_source_records(content, filename, extractor, *, source_id, observed_at=None,
                           file_extension=None, _archive_budget=None, _archive_depth=0):
    """Extract a source and ZIP members without writing archive paths to disk."""
    record = source_record(source_id, filename, content, source_type="attachment", observed_at=observed_at)
    records = [record]
    try:
        if (Path(filename).suffix.lower() == ".zip"
                or str(file_extension or "").strip().lower() in {"zip", ".zip"}):
            budget = _archive_budget if _archive_budget is not None else _ArchiveBudget()
            _extract_archive(content, filename, extractor, source_id, observed_at, budget, _archive_depth, records)
            record["status"] = ARCHIVE_EXPANDED_STATUS
        else:
            text = extract_document(content, filename, extractor)
            if _archive_budget is not None:
                _archive_budget.text_chars += len(text)
                if _archive_budget.text_chars > MAX_CONTEXT_CHARS:
                    raise SourceDocumentError("archive_text_limit", "ZIP document text exceeds the extraction context limit; split the archive.")
            record["text"] = text
            if Path(filename).suffix.lower() in IMAGE_EXTENSIONS:
                record["imageTextRegions"] = literal_image_regions(text)
            if Path(filename).suffix.lower() == ".pdf":
                # Independent physical metadata catches OCR confusions such as
                # ISO AB -> ISO A3. It flags review; it never overwrites a title block.
                with PdfReader(io.BytesIO(content)) as reader:
                    dimensions = [sorted(round(float(edge) * float(page.get('/UserUnit', 1)) * 25.4 / 72, 1)
                                         for edge in (page.mediabox.width, page.mediabox.height))
                                  for page in reader.pages]
                sizes = {"A0": (841, 1189), "A1": (594, 841), "A2": (420, 594),
                         "A3": (297, 420), "A4": (210, 297), "A5": (148, 210), "A6": (105, 148)}
                record["pdfPageDimensionsMm"] = dimensions
                record["pdfPageSizes"] = sorted({next((name for name, edges in sizes.items()
                    if all(abs(actual - expected) <= 3 for actual, expected in zip(pair, edges))), "Custom")
                    for pair in dimensions})
    except AnimatedImageSkipped:
        record["status"] = ANIMATED_IMAGE_SKIP_STATUS
    except DwgSkipped:
        record["status"] = DWG_SKIP_STATUS
    except EncryptedPdfSkipped:
        record["status"] = ENCRYPTED_PDF_SKIP_STATUS
    except Exception as exc:
        record["status"] = (PROVIDER_RECITATION_REVIEW_STATUS if _provider_recitation(exc)
                            else "failed:" + type(exc).__name__)
        error = source_failure_details(exc)
        if error:
            record["error"] = error
    return records


def _extract_archive(content, filename, extractor, source_id, observed_at, budget, depth, records):
    if len(content) > MAX_SOURCE_BYTES:
        raise SourceDocumentError("source_size_limit", "ZIP exceeds the 20 MiB source size limit; split the archive.")
    if depth >= MAX_ARCHIVE_DEPTH:
        raise SourceDocumentError("archive_depth_limit", "ZIP exceeds the three-level nesting limit; flatten the archive.")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            budget.members += len(entries)
            if budget.members > MAX_ARCHIVE_MEMBERS:
                raise SourceDocumentError("archive_member_limit", "ZIP exceeds the 200-entry limit including nested archives; split the archive.")
            files = [entry for entry in entries if not entry.is_dir()]
            if not files:
                raise SourceDocumentError("empty_archive", "ZIP contains no documents; supply an archive with readable documents.")
            if any(entry.file_size > MAX_SOURCE_BYTES for entry in files):
                raise SourceDocumentError("archive_member_size_limit", "ZIP contains a file exceeding the 20 MiB source size limit; supply smaller documents.")
            budget.expanded_bytes += sum(entry.file_size for entry in files)
            if budget.expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                raise SourceDocumentError("archive_expansion_limit", "ZIP exceeds the 100 MiB total expansion limit including nested archives; split the archive.")
            if any(entry.flag_bits & 1 for entry in files):
                raise SourceDocumentError("encrypted_archive", "ZIP contains encrypted files; supply an unencrypted archive.")
            for index, entry in enumerate(entries):
                if entry.is_dir():
                    continue
                # Read by ZipInfo, not name: duplicate filenames remain separate
                # evidence sources. The bound also protects against false headers.
                with archive.open(entry) as member:
                    member_content = member.read(MAX_SOURCE_BYTES + 1)
                if len(member_content) > MAX_SOURCE_BYTES or len(member_content) != entry.file_size:
                    raise SourceDocumentError("archive_member_size_limit", "ZIP member has an invalid or excessive expanded size; supply a valid, smaller archive.")
                children = extract_source_records(
                    member_content, f"{filename}!/{entry.filename}", extractor,
                    source_id=f"{source_id}:member:{index}", observed_at=observed_at,
                    _archive_budget=budget, _archive_depth=depth + 1,
                )
                children[0]["parentSourceId"] = source_id
                records.extend(children)
                if budget.text_chars > MAX_CONTEXT_CHARS:
                    raise SourceDocumentError("archive_text_limit", "ZIP document text exceeds the extraction context limit; split the archive.")
    except (BadZipFile, RuntimeError, NotImplementedError, zlib.error):
        raise SourceDocumentError("unreadable_archive", "ZIP is corrupt, encrypted, or uses unsupported compression; supply a readable ZIP archive.") from None


def source_record(source_id, filename, content, *, source_type, observed_at=None, text="", status="read", error=None):
    record = {
        "sourceId": source_id, "filename": filename, "sourceType": source_type,
        "contentSha256": hashlib.sha256(content).hexdigest(), "sizeBytes": len(content),
        "observedAt": observed_at, "status": status, "text": clean_source_text(text),
    }
    if error:
        record["error"] = error
    return record
