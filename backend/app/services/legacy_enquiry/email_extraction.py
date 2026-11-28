from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import hashlib
import io
import re
import quopri
from threading import Lock
from typing import Any, Optional, Protocol, Sequence, Union
from zoneinfo import ZoneInfo

import extract_msg

from . import source_context
from .source_documents import (
    IncompleteSourceExtraction, MAX_CONTEXT_CHARS, MAX_SOURCE_BYTES, SourceDocumentError,
    extract_source_records, html_text, source_record,
)
from .source_analysis_failures import SourceAnalysisReviewRequired
from ..specification_semantics import COMPLETE_SOURCE_STATUSES
from ..specification_request_context import attachment_message, parent_email, request_context_summary
from ..source_text import clean_source_text


MAX_ATTACHED_EMAIL_DEPTH = 3
MAX_ATTACHED_EMAIL_MESSAGES = 20
MAX_EMAIL_ATTACHMENTS = 200


def _mail_parts(message):
    """Walk one MIME message, never flattening attached messages into its owner."""
    pending = list(reversed(message.get_payload())) if message.is_multipart() else []
    visited = 0
    while pending:
        part = pending.pop()
        visited += 1
        if visited > MAX_EMAIL_ATTACHMENTS:
            raise SourceDocumentError("email_part_limit", "Email contains too many MIME parts; split the source.")
        yield part
        if part.get_content_type() == "message/rfc822":
            continue
        if part.is_multipart():
            pending.extend(reversed(part.get_payload()))


def _embedded_mime_bytes(part):
    payload = part.get_payload()
    if isinstance(payload, list) and len(payload) == 1:
        content = payload[0].as_bytes(policy=policy.default)
        transfer = str(part.get("Content-Transfer-Encoding", "")).casefold()
        if transfer == "base64":
            try:
                return base64.b64decode(b"".join(content.split()), validate=True)
            except (ValueError, binascii.Error):
                raise SourceDocumentError("unreadable_attached_email", "Attached email has invalid transfer encoding.") from None
        if transfer == "quoted-printable":
            return quopri.decodestring(content)
        return content
    decoded = part.get_payload(decode=True)
    if isinstance(decoded, bytes) and decoded:
        return decoded
    raise SourceDocumentError("unreadable_attached_email", "Attached email has an unreadable message payload.")


def _html_source_metadata(header, content):
    """Retain MIME HTML evidence, including CID references lost in plain text."""
    from bs4 import BeautifulSoup

    if not content:
        return {}
    text = clean_source_text(header) + "\n" + html_text(content)
    result = {"emailHtmlText": text, "emailHtmlSha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    # Mail gateways append the outer sender's legal footer after the original
    # Word email container. It is outside the forwarded message inside that div.
    soup = BeautifulSoup(content, "html.parser")
    containers = soup.body.find_all("div", class_="WordSection1", recursive=False) if soup.body else []
    if len(containers) == 1:
        footer = html_text("".join(str(node) for node in containers[0].next_siblings))
        if footer:
            # HTML parsing/serialization may normalize CRLF and NBSP. Recover
            # the actual contiguous suffix from the immutable extracted text.
            match = re.search(r"\s+".join(re.escape(word) for word in footer.split()) + r"\s*$", text)
            if match:
                result["emailOuterFooterText"] = match[0]
    return result


class AttachmentTextExtractor(Protocol):
    max_attachment_workers: int

    def should_batch_pdfs(self, pdf_files: Sequence[dict[str, Any]]) -> bool: ...

    def process_pdf(self, pdf_content: bytes, filename: str) -> str: ...

    def process_pdf_batch(self, pdf_files: Sequence[dict[str, Any]]) -> str: ...

    def process_image(
        self,
        image_content: bytes,
        filename: str,
        image_type: str = "ATTACHMENT",
    ) -> str: ...


def process_email_content(
    email_content: bytes,
    filename: str,
    *, source_metadata: dict | None = None,
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(email_content, bytes) or len(email_content) > MAX_SOURCE_BYTES:
        raise SourceDocumentError("email_size_limit", "Email exceeds the 40 MiB processing limit; supply smaller messages.")
    if source_metadata is not None:
        source_metadata.update(emailContainerSha256=hashlib.sha256(email_content).hexdigest(),
                               emailContainerSizeBytes=len(email_content))
    if filename.lower().endswith(".msg"):
        with io.BytesIO(email_content) as email_buffer:
            message = extract_msg.Message(email_buffer)
            try:
                header = (
                    f"From: {message.sender}\n"
                    f"To: {message.to}\n"
                    f"Subject: {message.subject}\n"
                    f"Date: {format_email_date(message.date or '')}\n"
                )
                body = message.body or html_text(message.htmlBody or b"")
                if source_metadata is not None:
                    source_metadata.update(_html_source_metadata(header, message.htmlBody))
                attachments: list[dict[str, Any]] = []
                inline_images: list[dict[str, Any]] = []
                for index, attachment in enumerate(message.attachments):
                    if index >= MAX_EMAIL_ATTACHMENTS:
                        raise SourceDocumentError("email_part_limit", "Email contains too many attachments; split the source.")
                    attachment_filename = (
                        attachment.longFilename or attachment.shortFilename
                    )
                    content = attachment.data
                    if not isinstance(content, bytes):
                        # extract_msg exposes embedded messages as MSGFile objects.
                        # Its byte exporter retains their own headers/attachments.
                        exporter = getattr(content, "exportBytes", None)
                        if exporter is None:
                            raise SourceDocumentError("unreadable_attached_email", "An Outlook embedded message cannot be exported for review.")
                        content = exporter()
                        attachment_filename = attachment_filename or f"attached-email-{index + 1}.msg"
                        if not attachment_filename.lower().endswith((".eml", ".msg")):
                            attachment_filename += ".msg"
                    if not attachment_filename:
                        raise SourceDocumentError("unnamed_email_attachment", "Email contains an unnamed attachment; supply a named source file.")
                    if is_inline_attachment(
                        attachment,
                        message,
                        attachment_filename,
                    ):
                        inline_images.append(
                            {
                                "filename": attachment_filename,
                                "content": content,
                                "content_id": getattr(attachment, "cid", None),
                                "mime_type": (
                                    f"image/{attachment_filename.split('.')[-1].lower()}"
                                ),
                            }
                        )
                    else:
                        attachments.append(
                            {
                                "filename": attachment_filename,
                                "content": content,
                            }
                        )
            finally:
                message.close()
    else:
        message = BytesParser(policy=policy.default).parsebytes(email_content)
        header = (
            f"From: {message.get('from', '')}\n"
            f"To: {message.get('to', '')}\n"
            f"Subject: {message.get('subject', '')}\n"
            f"Date: {format_email_date(message.get('date', ''))}\n"
        )
        # get_body respects multipart/alternative and avoids treating an attached
        # message as the current sender's body. Plain text is preferred to HTML.
        body_part = message.get_body(preferencelist=("plain", "html"))
        body = body_part.get_content() if body_part is not None else ""
        if not body.strip():
            body_part = message.get_body(preferencelist=("html",))
            body = body_part.get_content() if body_part is not None else ""
        if body_part is not None and body_part.get_content_type() == "text/html":
            body = html_text(body)
        if source_metadata is not None:
            html_part = message.get_body(preferencelist=("html",))
            if html_part is not None:
                source_metadata.update(_html_source_metadata(header, html_part.get_content()))

        attachments = []
        inline_images = []
        mime_defects = {type(defect).__name__ for defect in message.defects}
        attached_message_count = 0
        for part in _mail_parts(message):
            mime_defects.update(type(defect).__name__ for defect in part.defects)
            attachment_filename = part.get_filename()
            if part.get_content_type() == "message/rfc822":
                attached_message_count += 1
                attachments.append({
                    "filename": attachment_filename or f"attached-email-{attached_message_count}.eml",
                    "content": _embedded_mime_bytes(part), "mime_type": "message/rfc822",
                })
                continue
            if not attachment_filename and part.get_content_maintype() == "image":
                attachment_filename = "inline_image." + part.get_content_subtype()
            if not attachment_filename:
                continue
            content = part.get_payload(decode=True)
            if is_inline_image(part, attachment_filename):
                inline_images.append(
                    {
                        "filename": attachment_filename,
                        "content": content,
                        "content_id": part.get("Content-ID"),
                        "mime_type": part.get_content_type(),
                    }
                )
            else:
                attachments.append(
                    {"filename": attachment_filename, "content": content}
                )
        if mime_defects and source_metadata is not None:
            source_metadata["emailMimeDefects"] = sorted(mime_defects)
    return clean_source_text(header), clean_source_text(body), attachments, inline_images


def format_email_date(raw_date: Union[str, datetime]) -> str:
    if not raw_date:
        return raw_date
    try:
        parsed = (
            parsedate_to_datetime(raw_date)
            if isinstance(raw_date, str)
            else raw_date
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(ZoneInfo("Europe/London")).strftime(
            "%a, %d %b %Y %H:%M:%S %z"
        )
    except Exception:
        return str(raw_date)


def is_inline_image(part: Any, filename: str) -> bool:
    return filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp")) and bool(
        part.get("Content-ID")
    )


def is_inline_attachment(attachment: Any, message: Any, filename: str) -> bool:
    return filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp")) and (
        (hasattr(attachment, "cid") and attachment.cid)
        or (
            hasattr(message, "htmlBody")
            and message.htmlBody
            and filename
            in message.htmlBody.decode("utf-8", errors="ignore")
        )
    )


@dataclass
class _EmailBudget:
    messages: int = 0
    parsed_bytes: int = 0
    attachments: int = 0
    allow_large_context: bool = False
    text_chars: int = 0
    text_sources: set = field(default_factory=set)
    lock: Any = field(default_factory=Lock, repr=False)

    def message(self, content, depth):
        if depth > MAX_ATTACHED_EMAIL_DEPTH:
            raise SourceDocumentError("email_depth_limit", "Attached emails exceed the three-level nesting limit; flatten the messages.")
        if not isinstance(content, bytes) or not content:
            raise SourceDocumentError("unreadable_attached_email", "Attached email has no readable message bytes.")
        with self.lock:
            self.messages += 1
            self.parsed_bytes += len(content)
            if self.messages > MAX_ATTACHED_EMAIL_MESSAGES:
                raise SourceDocumentError("email_message_limit", "Email contains more than 20 attached messages; split the source.")
            if self.parsed_bytes > MAX_SOURCE_BYTES:
                raise SourceDocumentError("email_expansion_limit", "Attached email content exceeds the shared 40 MiB processing limit; split the source.")

    def attachment(self, count=1):
        with self.lock:
            self.attachments += count
            if self.attachments > MAX_EMAIL_ATTACHMENTS:
                raise SourceDocumentError("email_attachment_limit", "Email contains more than 200 attachments across nested messages; split the source.")

    def add_text(self, record):
        if record["status"] != "read":
            return
        source_id = record["sourceId"]
        with self.lock:
            if source_id in self.text_sources:
                return
            self.text_sources.add(source_id)
            self.text_chars += len(record["text"]) + len(record.get("emailHtmlText", ""))
            if not self.allow_large_context and self.text_chars > MAX_CONTEXT_CHARS:
                raise SourceDocumentError("email_text_limit", "Combined email content exceeds the extraction context limit; split the source.")


def _failed_email_record(source_id, filename, content, error, *, parent_source_id=None, attached=True):
    record = source_record(source_id, filename, content if isinstance(content, bytes) else b"",
        source_type="email_body" if attached else "attachment", text="",
        status="failed:SourceDocumentError", error=error.details)
    if parent_source_id:
        record["parentSourceId"] = parent_source_id
    if attached:
        record["attachedEmail"] = True
    return record


def _sent_at(header):
    value = next((line[6:] for line in header.splitlines() if line.startswith("Date: ")), "")
    try:
        stamp = parsedate_to_datetime(value)
        return stamp.replace(tzinfo=timezone.utc).isoformat() if stamp.tzinfo is None else stamp.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def extract_attached_email_records(content, filename, extractor, *, source_id, observed_at=None,
                                   parent_source_id=None, on_source=None, _email_budget=None,
                                   _email_depth=1, _archive_budget=None, _archive_depth=0):
    """Read an attached message with its own body identity and bounded children."""
    budget = _email_budget if _email_budget is not None else _EmailBudget()
    body_id = source_id + ":body"
    try:
        budget.message(content, _email_depth)
        metadata = {}
        header, body, attachments, images = process_email_content(content, filename, source_metadata=metadata)
        if set(metadata.get("emailMimeDefects", ())) & {
            "StartBoundaryNotFoundDefect", "CloseBoundaryNotFoundDefect", "NoBoundaryInMultipartDefect",
            "MissingHeaderBodySeparatorDefect", "MultipartInvariantViolationDefect",
        }:
            raise SourceDocumentError("unreadable_attached_email", "Attached message MIME structure is incomplete; supply a readable email file.")
        # The permissive MIME parser also accepts arbitrary text. Such bytes
        # cannot masquerade as a sourced attached request with invented headers.
        if not any(line.partition(": ")[2].strip() for line in header.splitlines()
                   if line.startswith(("From: ", "To: ", "Subject: "))):
            raise SourceDocumentError("unreadable_attached_email", "Attached message has no readable email headers.")
    except Exception as exc:
        error = exc if isinstance(exc, SourceDocumentError) else SourceDocumentError(
            "unreadable_attached_email", "Attached message cannot be parsed; supply a readable email file.")
        record = _failed_email_record(body_id, filename, content, error, parent_source_id=parent_source_id)
        if on_source:
            on_source(record)
        return [record]
    # Parsing failures above are source failures. Progress/cancellation callbacks
    # and downstream execution failures are not malformed message bytes.
    return _email_records(header + "\n" + body, attachments, extractor=extractor, inline_images=images,
        source_prefix=source_id, body_filename=filename, observed_at=_sent_at(header),
        source_metadata=metadata, parent_source_id=parent_source_id, attached=True,
        budget=budget, depth=_email_depth, on_source=on_source,
        archive_budget=_archive_budget, archive_depth=_archive_depth)


def email_body_record(email_text, source_id, *, observed_at, source_metadata, filename="Email body"):
    record = source_record(source_id, filename, email_text.encode("utf-8"),
        source_type="email_body", observed_at=observed_at, text=email_text)
    record.update(source_metadata or {})
    return record


def _email_records(email_text, attachments_data, *, extractor, inline_images, source_prefix, observed_at,
                   source_metadata, budget, depth=0, parent_source_id=None, attached=False,
                   body_filename="Email body", on_source=None, archive_budget=None, archive_depth=0):
    body_id = source_prefix + ":body"
    record = email_body_record(email_text, body_id, observed_at=observed_at,
                               source_metadata=source_metadata, filename=body_filename)
    if parent_source_id:
        record["parentSourceId"] = parent_source_id
    if attached:
        record["attachedEmail"] = True
    records = [record]
    try:
        budget.add_text(record)
    except SourceDocumentError as exc:
        record.update(status="failed:SourceDocumentError", text="", error=exc.details)
        if on_source:
            on_source(record)
        return records
    attachments = list(attachments_data) + list(inline_images or ())
    if on_source:
        on_source(record)
        for index in range(len(attachments)):
            attachment = attachments[index]
            is_email = (attachment["filename"].lower().endswith((".eml", ".msg"))
                        or attachment.get("mime_type") == "message/rfc822")
            on_source({"sourceId": f"{source_prefix}:attachment:{index}" + (":body" if is_email else ""),
                       "status": "pending"})
    results, jobs = {}, {}
    for index, attachment in enumerate(attachments):
        identity = f"{source_prefix}:attachment:{index}"
        filename = attachment["filename"]
        content = attachment.get("content")
        is_email = filename.lower().endswith((".eml", ".msg")) or attachment.get("mime_type") == "message/rfc822"
        try:
            budget.attachment()
        except SourceDocumentError as exc:
            results[index] = [_failed_email_record(identity + (":body" if is_email else ""),
                filename, content, exc, parent_source_id=body_id, attached=is_email)]
            continue
        if is_email:
            results[index] = extract_attached_email_records(content, filename, extractor,
                source_id=identity, parent_source_id=body_id, on_source=on_source,
                _email_budget=budget, _email_depth=depth + 1,
                _archive_budget=archive_budget, _archive_depth=archive_depth)
        else:
            jobs[index] = attachment

    def read_document(index, attachment):
        return extract_source_records(attachment["content"], attachment["filename"], extractor,
            source_id=f"{source_prefix}:attachment:{index}", observed_at=observed_at,
            on_source=on_source, _email_budget=budget, _email_depth=depth,
            _archive_budget=archive_budget, _archive_depth=archive_depth)

    if jobs:
        # An existing ZIP traversal owns one shared expansion counter. Keep its
        # descendants sequential instead of racing that counter across threads.
        if archive_budget is not None:
            for index, attachment in jobs.items():
                results[index] = read_document(index, attachment)
        else:
            with ThreadPoolExecutor(max_workers=min(getattr(extractor, "max_attachment_workers", 4), len(jobs))) as executor:
                futures = {executor.submit(read_document, index, attachment): index for index, attachment in jobs.items()}
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
    for index in range(len(attachments)):
        first = results[index][0]
        first.setdefault("parentSourceId", body_id)
        content_id = attachments[index].get("content_id")
        if content_id:
            first["contentId"] = str(content_id).strip("<>")
        if index in jobs:
            try:
                for entry in results[index]:
                    if entry.get("sourceType") != "email_body":
                        budget.add_text(entry)
            except SourceDocumentError as exc:
                first.update(status="failed:SourceDocumentError", text="", error=exc.details)
        records.extend(results[index])
        if on_source:
            on_source(first)
    return records


def extract_text_from_email(
    email_text: str,
    attachments_data: Sequence[dict[str, Any]],
    *,
    extractor: AttachmentTextExtractor,
    inline_images: Optional[Sequence[dict[str, Any]]] = None,
    manifest: Optional[list[dict[str, Any]]] = None,
    source_prefix: str = "email",
    observed_at: str | None = None,
    source_metadata: dict | None = None,
    on_source=None,
    allow_large_context: bool = False,
) -> str:
    budget = _EmailBudget(parsed_bytes=(source_metadata or {}).get("emailContainerSizeBytes", 0),
                          allow_large_context=allow_large_context)
    records = _email_records(email_text, attachments_data, extractor=extractor,
        inline_images=inline_images, source_prefix=source_prefix, observed_at=observed_at,
        source_metadata=source_metadata, budget=budget, on_source=on_source)
    sources = {record["sourceId"]: record for record in records}
    for record in records:
        containing = attachment_message(record, sources)
        if containing:
            parent = parent_email(record, sources)
            record["containingMessage"] = {
                **request_context_summary(containing), "sourceId": parent["sourceId"],
                "contentSha256": parent["contentSha256"],
            }
    if manifest is not None:
        manifest.extend(records)
    if any(record["status"] not in COMPLETE_SOURCE_STATUSES for record in records):
        raise IncompleteSourceExtraction(records)
    # Raw text alone cannot fit beyond this bound. The item batcher also checks
    # rendered headings, source boundaries, and overlapping excerpts.
    batch_limit = source_context.MAX_CONTEXT_BATCHES
    if allow_large_context and budget.text_chars > MAX_CONTEXT_CHARS * batch_limit:
        raise source_context.source_context_review(records, limit=MAX_CONTEXT_CHARS,
            character_count=budget.text_chars, batch_limit=batch_limit)
    combined_text = "\n\n".join("".join(source_context.source_context_parts(record))
                                for record in records if record["status"] == "read")
    if not allow_large_context and len(combined_text) > MAX_CONTEXT_CHARS:
        raise SourceAnalysisReviewRequired("Combined source text exceeds extraction limit; review required",
            code="combined_source_text_limit", scope="email", source_id=f"{source_prefix}:body",
            character_count=len(combined_text), character_limit=MAX_CONTEXT_CHARS)
    return combined_text
