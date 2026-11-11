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
    ENCRYPTED_PDF_SKIP_STATUS, PROVIDER_RECITATION_REVIEW_STATUS, UNSUPPORTED_SOURCE_SKIP_STATUS,
)
from ..specification_image_text import literal_image_regions
from ..source_text import clean_source_text


MAX_SOURCE_BYTES = 40 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200
MAX_ARCHIVE_DEPTH = 3
MAX_CONTEXT_CHARS = 500_000
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
DOCUMENT_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".txt", ".csv", ".tsv", ".json", ".xml", ".html", ".htm",
    *IMAGE_EXTENSIONS,
})


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
        super().__init__("unsupported_source_format", "Unsupported source format; skipped during extraction.")


def _document_skip_status(filename):
    suffix = Path(filename).suffix.lower()
    if suffix == ".dwg":
        return DWG_SKIP_STATUS
    if suffix not in DOCUMENT_EXTENSIONS and not is_spreadsheet(filename):
        return UNSUPPORTED_SOURCE_SKIP_STATUS
    return None


def _archive_member_skip_status(filename):
    return None if Path(filename).suffix.lower() == ".zip" else _document_skip_status(filename)


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


def extract_document(content: bytes, filename: str, extractor, *, extraction_metadata=None) -> str:
    suffix = Path(filename).suffix.lower()
    skip_status = _document_skip_status(filename)
    if skip_status == DWG_SKIP_STATUS:
        raise DwgSkipped("DWG drawings are excluded from extraction")
    if skip_status == UNSUPPORTED_SOURCE_SKIP_STATUS:
        raise UnsupportedSourceFormat()
    if len(content) > MAX_SOURCE_BYTES:
        raise SourceDocumentError("source_size_limit", "Source exceeds the 40 MiB extraction size limit; supply a smaller document.")
    if is_spreadsheet(filename):
        with tempfile.TemporaryDirectory(prefix="design-sheet-") as directory:
            path = Path(directory) / ("source" + suffix)
            path.write_bytes(content)
            documents = extract_spreadsheet_documents(str(path), filename, visible_only=True, preserve_full_cells=True,
                                                      max_file_bytes=MAX_SOURCE_BYTES)
        notices = [doc["notice"] for doc in documents if doc["section"] == "spreadsheet:extraction-notice"]
        if notices:
            raise SourceDocumentError("workbook_incomplete", "Workbook extraction incomplete: " + " ".join(notices)
                                      + " Recalculate and save uncached formulas in Excel, or supply a smaller readable export.")
        text = "\n\n".join(doc["text"] for doc in documents)
    elif suffix == ".pdf":
        native_text = None
        try:
            with PdfReader(io.BytesIO(content)) as reader:
                # Skip all encryption, including owner-only restrictions with
                # an empty opening password. Never send these bytes to Gemini.
                if reader.is_encrypted:
                    raise EncryptedPdfSkipped("Encrypted PDFs are excluded from extraction")
                if getattr(extractor, "pdf_native_text_enabled", False):
                    from .native_pdf_text import native_pdf_text
                    native_result = native_pdf_text(reader)
                    native_text = native_result.text
                    if extraction_metadata is not None:
                        extraction_metadata["pdfTextMethod"] = "native" if native_text is not None else "gemini"
                        if native_text is None:
                            extraction_metadata["pdfNativeTextFallbackReason"] = native_result.reason
        except PdfReadError:
            raise SourceDocumentError("unreadable_pdf", "PDF could not be read; supply a valid PDF export.") from None
        # Per-document calls retain identity and make individual failures retryable.
        text = native_text if native_text is not None else extractor.process_pdf(content, filename)
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
                           file_extension=None, _archive_budget=None, _archive_depth=0, on_source=None):
    """Extract a source and ZIP members without writing archive paths to disk."""
    record = source_record(source_id, filename, content, source_type="attachment", observed_at=observed_at)
    records = [record]
    try:
        if (Path(filename).suffix.lower() == ".zip"
                or str(file_extension or "").strip().lower() in {"zip", ".zip"}):
            budget = _archive_budget if _archive_budget is not None else _ArchiveBudget()
            _extract_archive(content, filename, extractor, source_id, observed_at, budget, _archive_depth, records, on_source)
            record["status"] = ARCHIVE_EXPANDED_STATUS
        else:
            text = extract_document(content, filename, extractor, extraction_metadata=record)
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
    except UnsupportedSourceFormat:
        record["status"] = UNSUPPORTED_SOURCE_SKIP_STATUS
    except EncryptedPdfSkipped:
        record["status"] = ENCRYPTED_PDF_SKIP_STATUS
    except Exception as exc:
        record["status"] = (PROVIDER_RECITATION_REVIEW_STATUS if _provider_recitation(exc)
                            else "failed:" + type(exc).__name__)
        error = source_failure_details(exc)
        if error:
            record["error"] = error
    if on_source is not None:
        on_source(record)
    return records


def _extract_archive(content, filename, extractor, source_id, observed_at, budget, depth, records, on_source=None):
    if len(content) > MAX_SOURCE_BYTES:
        raise SourceDocumentError("source_size_limit", "ZIP exceeds the 40 MiB source size limit; split the archive.")
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
            if any(entry.file_size > MAX_SOURCE_BYTES and _archive_member_skip_status(entry.filename) is None
                   for entry in files):
                raise SourceDocumentError("archive_member_size_limit", "ZIP contains a supported file exceeding the 40 MiB source size limit; supply smaller documents.")
            budget.expanded_bytes += sum(entry.file_size for entry in files)
            if budget.expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                raise SourceDocumentError("archive_expansion_limit", "ZIP exceeds the 100 MiB total expansion limit including nested archives; split the archive.")
            if any(entry.flag_bits & 1 for entry in files):
                raise SourceDocumentError("encrypted_archive", "ZIP contains encrypted files; supply an unencrypted archive.")
            if on_source is not None:
                on_source({"sourceId": source_id, "status": ARCHIVE_EXPANDED_STATUS})
                for index, entry in enumerate(entries):
                    if not entry.is_dir():
                        on_source({"sourceId": f"{source_id}:member:{index}", "status": "pending"})
            for index, entry in enumerate(entries):
                if entry.is_dir():
                    continue
                # Read by ZipInfo, not name: duplicate filenames remain separate
                # evidence sources. Skipped formats are read only for their audit
                # hash, bounded by the existing total archive expansion budget.
                # Their size must not block readable documents in the archive.
                read_limit = (MAX_SOURCE_BYTES if _archive_member_skip_status(entry.filename) is None
                              else MAX_ARCHIVE_EXPANDED_BYTES)
                with archive.open(entry) as member:
                    member_content = member.read(read_limit + 1)
                if len(member_content) > read_limit or len(member_content) != entry.file_size:
                    raise SourceDocumentError("archive_member_size_limit", "ZIP member has an invalid or excessive expanded size; supply a valid, smaller archive.")
                children = extract_source_records(
                    member_content, f"{filename}!/{entry.filename}", extractor,
                    source_id=f"{source_id}:member:{index}", observed_at=observed_at,
                    _archive_budget=budget, _archive_depth=depth + 1,
                    on_source=on_source,
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
