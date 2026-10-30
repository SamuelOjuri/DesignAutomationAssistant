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
from defusedxml import ElementTree
from zipfile import BadZipFile, ZipFile
import zlib

from ..gemini_requests import source_failure_details as gemini_failure_details
from ..spreadsheet_extraction import extract_spreadsheet_documents, is_spreadsheet
from ..specification_semantics import (
    ANIMATED_IMAGE_SKIP_STATUS, ARCHIVE_EXPANDED_STATUS, COMPLETE_SOURCE_STATUSES, DWG_SKIP_STATUS,
)


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


def _failure_summary(source):
    summary = f"{source['filename']}: {source['status']}"
    error = source.get("error")
    if isinstance(error, dict):
        if error.get("stage") == "source_document":
            summary += f" ({error['message']})"
        else:
            summary += f" (stage={error.get('stage')}, http_status={error.get('http_status')}, mode={error.get('mode')})"
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
    suffix = Path(filename).suffix.lower()
    if suffix == ".dwg":
        raise DwgSkipped("DWG drawings are excluded from extraction")
    if len(content) > MAX_SOURCE_BYTES:
        raise SourceDocumentError("source_size_limit", "Source exceeds the 20 MiB extraction size limit; supply a smaller document.")
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
        raise ValueError("Document extraction returned no readable text")
    if len(text) > MAX_CONTEXT_CHARS:
        raise ValueError("Source exceeds the extracted text limit")
    return text


@dataclass
class _ArchiveBudget:
    members: int = 0
    expanded_bytes: int = 0
    text_chars: int = 0


def extract_source_records(content, filename, extractor, *, source_id, observed_at=None,
                           _archive_budget=None, _archive_depth=0):
    """Extract a source and ZIP members without writing archive paths to disk."""
    record = source_record(source_id, filename, content, source_type="attachment", observed_at=observed_at)
    records = [record]
    try:
        if Path(filename).suffix.lower() == ".zip":
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
    except AnimatedImageSkipped:
        record["status"] = ANIMATED_IMAGE_SKIP_STATUS
    except DwgSkipped:
        record["status"] = DWG_SKIP_STATUS
    except Exception as exc:
        record["status"] = "failed:" + type(exc).__name__
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
        "observedAt": observed_at, "status": status, "text": text,
    }
    if error:
        record["error"] = error
    return record
