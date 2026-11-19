"""Source-labelled, bounded attachment extraction shared with task ingestion.

Every readable source has its own text and hash. Failures are not negative
evidence: callers retain their previous successful output and retry extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import io
import logging
from pathlib import Path
import tempfile
from zipfile import BadZipFile, ZipFile
import zlib

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import NameObject

from ..gemini_requests import GeminiRequestFailure, source_failure_details as gemini_failure_details
from ..spreadsheet_extraction import extract_spreadsheet_documents, is_spreadsheet
from ..specification_semantics import (
    ANIMATED_IMAGE_SKIP_STATUS, ARCHIVE_EXPANDED_STATUS, ARCHIVE_MEMBER_REVIEW_STATUS,
    COMPLETE_SOURCE_STATUSES, DOCX_EXPANDED_STATUS, DWG_SKIP_STATUS,
    ENCRYPTED_PDF_SKIP_STATUS, OVERSIZED_ARCHIVE_REVIEW_STATUS,
    PROVIDER_RECITATION_REVIEW_STATUS, UNSUPPORTED_SOURCE_SKIP_STATUS,
)
from ..specification_image_text import literal_image_regions
from ..source_text import clean_source_text
from .docx_documents import UnreadableDocxDrawing, read_docx
from .native_pdf_text import MAX_PDF_PAGES, native_pdf_text


logger = logging.getLogger(__name__)
MAX_SOURCE_BYTES = 40 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200
MAX_ARCHIVE_DEPTH = 3
MAX_CONTEXT_CHARS = 500_000
# Contract pricing workbooks commonly have one sheet per trade package. Row,
# cell, chunk and extracted-text limits still bound the total work.
MAX_DESIGN_WORKSHEETS = 40
# Rejections of the downloaded bytes themselves recur on every attempt; only a
# replacement source (a new input revision) can change them. Model-dependent
# outcomes such as no_readable_text and transcribed-text limits stay retryable.
PERMANENT_SOURCE_ERROR_CODES = frozenset({
    "source_size_limit", "workbook_incomplete", "unreadable_pdf", "unreadable_archive",
    "empty_archive", "encrypted_archive", "archive_depth_limit", "archive_member_limit",
    "archive_member_size_limit", "archive_expansion_limit",
    "email_size_limit", "email_depth_limit", "email_message_limit", "email_expansion_limit",
    "email_attachment_limit", "email_part_limit", "email_text_limit", "unreadable_attached_email",
    "unnamed_email_attachment", "invalid_docx_drawing", "unsupported_docx_drawing",
})
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
DOCUMENT_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".txt", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".eml", ".msg",
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


class OversizedArchiveReview(SourceDocumentError):
    """An unchanged ZIP size policy requires manual source review, not a retry."""


def _oversized_archive_error():
    return OversizedArchiveReview("source_size_limit",
        "ZIP exceeds the 40 MiB processing limit and was not extracted. "
        "Review the original ZIP manually and record necessary specification corrections, "
        "or supply smaller files for processing.")


def _mark_archive_review(record, error):
    record["status"] = OVERSIZED_ARCHIVE_REVIEW_STATUS
    record["error"] = error.details
    logger.info("ZIP requires manual review: source_id=%s size_bytes=%s limit_bytes=%s",
                record["sourceId"], record["sizeBytes"], MAX_SOURCE_BYTES)
    return record


def oversized_archive_file_record(path, filename, *, source_id, file_extension=None):
    """Audit an oversized downloaded ZIP without loading its bytes into memory.

    The caller verifies the streamed hash/size against its download identity.
    The archive is never opened; this is a manual-review referral, not extraction.
    """
    if (Path(filename).suffix.lower() != ".zip"
            and str(file_extension or "").strip().lower() not in {"zip", ".zip"}):
        return None
    path = Path(path)
    if path.stat().st_size <= MAX_SOURCE_BYTES:
        return None
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return _mark_archive_review({
        "sourceId": source_id, "filename": filename, "sourceType": "attachment",
        "contentSha256": digest.hexdigest(), "sizeBytes": size,
        "observedAt": None, "text": "",
    }, _oversized_archive_error())


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


def _archive_member_metadata_only(filename):
    # These unsupported TaperedView mesh/material files are retained for manual
    # inspection. Reading their uncompressed bytes would add no model evidence.
    # Other unsupported formats keep the existing hashed/expansion-bounded path.
    return (Path(filename).suffix.lower() in {".obj", ".mtl"}
            and _archive_member_skip_status(filename) == UNSUPPORTED_SOURCE_SKIP_STATUS)


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
    import re
    from ..database_diagnostics import sanitized_database_details

    summary = f"{source['filename']}: {source['status']}"
    error = source.get("error")
    if isinstance(error, dict):
        if error.get("stage") == "source_document":
            summary += f" ({error['message']})"
        else:
            summary += f" (stage={error.get('stage')}, http_status={error.get('http_status')}, mode={error.get('mode')})"
            if error.get("code"):
                summary += f" (code={error['code']}, finish_reason={error.get('finish_reason')})"
            diagnostics = {}
            error_type = error.get("error_type")
            if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error_type):
                diagnostics["error_type"] = error_type
            diagnostics.update(sanitized_database_details(error) or {})
            if error.get("origin") in {"gemini_admission", "database"}:
                diagnostics["origin"] = error["origin"]
            if diagnostics:
                summary += " (" + ", ".join(f"{key}={value}" for key, value in diagnostics.items()) + ")"
    return summary


class IncompleteSourceExtraction(ValueError):
    def __init__(self, manifest):
        self.manifest = manifest
        failures = [_failure_summary(source) for source in manifest
                    if source["status"] not in COMPLETE_SOURCE_STATUSES]
        super().__init__("Incomplete source extraction; retry/review required: " + "; ".join(failures)[:1000])


def permanent_source_failure(manifest) -> bool:
    """True when every incomplete source failed on its own unchanged bytes."""
    failed = [source for source in manifest or []
              if isinstance(source, dict) and source.get("status") not in COMPLETE_SOURCE_STATUSES]
    return bool(failed) and all(
        isinstance(source.get("error"), dict) and source["error"].get("stage") == "source_document"
        and source["error"].get("code") in PERMANENT_SOURCE_ERROR_CODES
        for source in failed
    )


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


def _record_pdf_page_sizes(pages, metadata):
    # Physical sizes can flag OCR mistakes such as ISO AB -> ISO A3.
    dimensions = [sorted(round(float(edge) * float(page.get("/UserUnit", 1)) * 25.4 / 72, 1)
                         for edge in (page.mediabox.width, page.mediabox.height)) for page in pages]
    sizes = {"A0": (841, 1189), "A1": (594, 841), "A2": (420, 594),
             "A3": (297, 420), "A4": (210, 297), "A5": (148, 210), "A6": (105, 148)}
    metadata["pdfPageDimensionsMm"] = dimensions
    metadata["pdfPageSizes"] = sorted({next((name for name, edges in sizes.items()
        if all(abs(actual - expected) <= 3 for actual, expected in zip(pair, edges))), "Custom")
        for pair in dimensions})


def _extract_pdf(content, filename, extractor, metadata):
    try:
        with PdfReader(io.BytesIO(content)) as reader:
            # Skip all encryption, including owner-only restrictions with an empty opening password.
            if reader.is_encrypted:
                raise EncryptedPdfSkipped("Encrypted PDFs are excluded from extraction")
            page_count = len(reader.pages)
            pages_read = min(page_count, MAX_PDF_PAGES)
            if metadata is not None:
                if pages_read < page_count:
                    metadata.update(pdfPageCount=page_count, pdfPagesRead=pages_read)
                _record_pdf_page_sizes(reader.pages[:pages_read], metadata)
            if getattr(extractor, "pdf_native_text_enabled", False):
                result = native_pdf_text(reader)
                if metadata is not None:
                    metadata["pdfTextMethod"] = "native" if result.text is not None else "gemini"
                    if result.text is None:
                        metadata["pdfNativeTextFallbackReason"] = result.reason
                if result.text is not None:
                    return result.text
            if pages_read < page_count:
                with io.BytesIO() as output, PdfWriter() as writer:
                    writer.append(reader, pages=(0, pages_read), import_outline=False)
                    # Preserve the original visibility configuration for layered drawings.
                    if "/OCProperties" in reader.root_object:
                        writer.root_object[NameObject("/OCProperties")] = reader.root_object["/OCProperties"].clone(writer)
                    writer.write(output)
                    content = output.getvalue()
    except PdfReadError:
        raise SourceDocumentError("unreadable_pdf", "PDF could not be read; supply a valid PDF export.") from None
    return extractor.process_pdf(content, filename)


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
                                                      max_file_bytes=MAX_SOURCE_BYTES,
                                                      max_sheets=MAX_DESIGN_WORKSHEETS, truncate_sheets=True)
        if extraction_metadata is not None and any(
                doc["section"] == "spreadsheet:worksheet-limit" for doc in documents):
            extraction_metadata["spreadsheetExtraction"] = {
                "worksheetLimit": MAX_DESIGN_WORKSHEETS, "visibleOnly": True, "truncated": True,
            }
        notices = [doc["notice"] for doc in documents if doc["section"] == "spreadsheet:extraction-notice"]
        if notices:
            advice = ("Recalculate and save uncached formulas in Excel, or supply a smaller readable export."
                      if any("formula" in notice.lower() for notice in notices)
                      else "Supply a smaller readable export, or split the workbook.")
            raise SourceDocumentError("workbook_incomplete",
                                      "Workbook extraction incomplete: " + " ".join(notices) + " " + advice)
        text = "\n\n".join(doc["text"] for doc in documents)
    elif suffix == ".pdf":
        text = _extract_pdf(content, filename, extractor, extraction_metadata)
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
        parent = source_record("document", filename, content, source_type="attachment")
        records = [parent, *_extract_docx_drawings(content, extractor, parent)]
        if any(record["status"] not in COMPLETE_SOURCE_STATUSES for record in records):
            raise IncompleteSourceExtraction(records)
        text = "\n\n".join(record["text"] for record in records if record["status"] == "read")
    elif suffix in {".txt", ".csv", ".tsv", ".json", ".xml"}:
        text = content.decode("utf-8-sig")
    elif suffix in {".html", ".htm"}:
        text = html_text(content)
    else:
        raise UnsupportedSourceFormat()
    return _checked_document_text(text)


def _checked_document_text(text):
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

    def add_members(self, count):
        self.members += count
        if self.members > MAX_ARCHIVE_MEMBERS:
            raise SourceDocumentError("archive_member_limit", "ZIP exceeds the 200-entry limit including nested archives; split the archive.")

    def expand(self, size):
        self.expanded_bytes += size
        if self.expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
            raise SourceDocumentError("archive_expansion_limit", "ZIP exceeds the 100 MiB total expansion limit including nested archives; split the archive.")

    def add_text(self, text):
        self.text_chars += len(text)
        if self.text_chars > MAX_CONTEXT_CHARS:
            raise SourceDocumentError("archive_text_limit", "ZIP document text exceeds the extraction context limit; split the archive.")


def _extract_docx_drawings(content, extractor, parent, *, archive_budget=None, email_budget=None, on_source=None):
    if len(content) > MAX_SOURCE_BYTES:
        raise SourceDocumentError("source_size_limit", "Source exceeds the 40 MiB extraction size limit; supply a smaller document.")
    try:
        text, drawings = read_docx(content, max_expanded_bytes=MAX_ARCHIVE_EXPANDED_BYTES,
                                   max_image_bytes=MAX_SOURCE_BYTES, max_drawings=MAX_ARCHIVE_MEMBERS)
    except UnreadableDocxDrawing as exc:
        raise SourceDocumentError("invalid_docx_drawing", str(exc)) from None

    if drawings and email_budget is not None:
        email_budget.attachment(len(drawings))
    if drawings and archive_budget is not None:
        archive_budget.add_members(len(drawings))
        archive_budget.expand(sum(size for _, size, _ in drawings))
    if drawings and not text.strip():
        parent["status"] = DOCX_EXPANDED_STATUS
    else:
        parent["text"] = _checked_document_text(text)
    if archive_budget is not None:
        archive_budget.add_text(parent["text"])

    children = []
    source_id = parent["sourceId"]
    if drawings and on_source is not None:
        on_source(parent)
        for index in range(len(drawings)):
            on_source({"sourceId": f"{source_id}:drawing:{index}", "status": "pending"})
    for index, (part, size, image) in enumerate(drawings):
        identity = f"{source_id}:drawing:{index}"
        filename = f"{parent['filename']}!/{part}"
        observed_at = parent["observedAt"]
        if image is not None and Path(part).suffix.lower() in IMAGE_EXTENSIONS:
            child, = extract_source_records(image, filename, extractor, source_id=identity,
                                            observed_at=observed_at, _archive_budget=archive_budget)
        else:
            error = (SourceDocumentError("source_size_limit", "Embedded Word drawing exceeds 40 MiB; supply a smaller image.")
                     if image is None else SourceDocumentError("unsupported_docx_drawing",
                         "Word drawing uses an unsupported image format. Export the drawing as PNG or JPEG."))
            child = source_record(identity, filename, image or b"", source_type="attachment",
                                  observed_at=observed_at, status="failed:SourceDocumentError", error=error.details)
            if image is None:
                child.update(contentSha256=None, sizeBytes=size)
        child.update(parentSourceId=source_id, docxDrawingPart=part)
        children.append(child)
        if on_source is not None:
            on_source(child)
    return children


def extract_source_records(content, filename, extractor, *, source_id, observed_at=None,
                           file_extension=None, _archive_budget=None, _archive_depth=0, on_source=None,
                           _email_budget=None, _email_depth=0):
    """Extract a source and ZIP members without writing archive paths to disk."""
    if Path(filename).suffix.lower() in {".eml", ".msg"}:
        # The bounded parser audits byte/format failures itself. Keep progress
        # cancellation outside the document reader's parser-error conversion.
        from .email_extraction import extract_attached_email_records
        return extract_attached_email_records(content, filename, extractor,
            source_id=source_id, observed_at=observed_at, on_source=on_source,
            _email_budget=_email_budget, _email_depth=_email_depth + 1,
            _archive_budget=_archive_budget, _archive_depth=_archive_depth)
    record = source_record(source_id, filename, content, source_type="attachment", observed_at=observed_at)
    records = [record]
    try:
        if (Path(filename).suffix.lower() == ".zip"
                or str(file_extension or "").strip().lower() in {"zip", ".zip"}):
            if _email_budget is None:
                from .email_extraction import _EmailBudget
                _email_budget = _EmailBudget()
            budget = _archive_budget if _archive_budget is not None else _ArchiveBudget()
            _extract_archive(content, filename, extractor, source_id, observed_at, budget, _archive_depth,
                             records, on_source, archive_sha256=record["contentSha256"],
                             email_budget=_email_budget, email_depth=_email_depth)
            record["status"] = ARCHIVE_EXPANDED_STATUS
        elif Path(filename).suffix.lower() == ".docx":
            records.extend(_extract_docx_drawings(content, extractor, record, archive_budget=_archive_budget,
                                                 email_budget=_email_budget, on_source=on_source))
        else:
            text = extract_document(content, filename, extractor, extraction_metadata=record)
            if _archive_budget is not None:
                _archive_budget.add_text(text)
            record["text"] = text
            if Path(filename).suffix.lower() in IMAGE_EXTENSIONS:
                record["imageTextRegions"] = literal_image_regions(text)
    except AnimatedImageSkipped:
        record["status"] = ANIMATED_IMAGE_SKIP_STATUS
    except DwgSkipped:
        record["status"] = DWG_SKIP_STATUS
    except UnsupportedSourceFormat:
        record["status"] = UNSUPPORTED_SOURCE_SKIP_STATUS
    except EncryptedPdfSkipped:
        record["status"] = ENCRYPTED_PDF_SKIP_STATUS
    except OversizedArchiveReview as exc:
        _mark_archive_review(record, exc)
    except Exception as exc:
        record["status"] = (PROVIDER_RECITATION_REVIEW_STATUS if _provider_recitation(exc)
                            else "failed:" + type(exc).__name__)
        error = source_failure_details(exc)
        if error:
            record["error"] = error
    if on_source is not None:
        on_source(record)
    return records


def _extract_archive(content, filename, extractor, source_id, observed_at, budget, depth, records,
                     on_source=None, *, archive_sha256, email_budget=None, email_depth=0):
    if len(content) > MAX_SOURCE_BYTES:
        raise _oversized_archive_error()
    if depth >= MAX_ARCHIVE_DEPTH:
        raise SourceDocumentError("archive_depth_limit", "ZIP exceeds the three-level nesting limit; flatten the archive.")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            budget.add_members(len(entries))
            files = [entry for entry in entries if not entry.is_dir()]
            if not files:
                raise SourceDocumentError("empty_archive", "ZIP contains no documents; supply an archive with readable documents.")
            oversized = [entry for entry in files if entry.file_size > MAX_SOURCE_BYTES
                         and _archive_member_skip_status(entry.filename) is None]
            if oversized:
                if all(Path(entry.filename).suffix.lower() == ".zip" for entry in oversized):
                    raise OversizedArchiveReview("archive_member_size_limit",
                        "ZIP contains a nested ZIP exceeding the 40 MiB processing limit and was not extracted. "
                        "Review the original ZIP manually and record necessary specification corrections, "
                        "or supply smaller files for processing.")
                raise SourceDocumentError("archive_member_size_limit", "ZIP contains a supported file exceeding the 40 MiB source size limit; supply smaller documents.")
            # Only OBJ/MTL entries below stay compressed and therefore incur no
            # expansion work. All other members, including ordinary unsupported
            # formats, still count against the unchanged shared byte allowance.
            budget.expand(sum(entry.file_size for entry in files if not _archive_member_metadata_only(entry.filename)))
            if any(entry.flag_bits & 1 for entry in files):
                raise SourceDocumentError("encrypted_archive", "ZIP contains encrypted files; supply an unencrypted archive.")
            if on_source is not None:
                on_source({"sourceId": source_id, "status": ARCHIVE_EXPANDED_STATUS})
                for index, entry in enumerate(entries):
                    if not entry.is_dir():
                        suffix = ":body" if Path(entry.filename).suffix.lower() in {".eml", ".msg"} else ""
                        on_source({"sourceId": f"{source_id}:member:{index}" + suffix, "status": "pending"})
            for index, entry in enumerate(entries):
                if entry.is_dir():
                    continue
                if _archive_member_metadata_only(entry.filename):
                    # No member hash or CRC validation is claimed: the identity
                    # is the verified containing archive plus its central-directory
                    # index/CRC and declared size. Duplicate names remain distinct.
                    record = {
                        "sourceId": f"{source_id}:member:{index}",
                        "filename": f"{filename}!/{entry.filename}", "sourceType": "attachment",
                        "contentSha256": None, "sizeBytes": entry.file_size,
                        "observedAt": observed_at, "status": ARCHIVE_MEMBER_REVIEW_STATUS, "text": "",
                        "parentSourceId": source_id, "archiveContentSha256": archive_sha256,
                        "archiveMemberIndex": index, "archiveMemberCrc32": format(entry.CRC, "08x"),
                        "archiveMemberSizeVerified": False,
                    }
                    records.append(record)
                    if on_source is not None:
                        on_source(record)
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
                    _email_budget=email_budget, _email_depth=email_depth,
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
