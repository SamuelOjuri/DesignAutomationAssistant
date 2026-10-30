from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import io
from typing import Any, Optional, Protocol, Sequence, Union
from zoneinfo import ZoneInfo

import extract_msg

from .source_documents import (
    AnimatedImageSkipped, IncompleteSourceExtraction, MAX_CONTEXT_CHARS,
    extract_document, html_text, source_record,
)
from ..specification_semantics import ANIMATED_IMAGE_SKIP_STATUS, COMPLETE_SOURCE_STATUSES


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
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
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
                attachments: list[dict[str, Any]] = []
                inline_images: list[dict[str, Any]] = []
                for attachment in message.attachments:
                    attachment_filename = (
                        attachment.longFilename or attachment.shortFilename
                    )
                    if not attachment_filename:
                        continue
                    if is_inline_attachment(
                        attachment,
                        message,
                        attachment_filename,
                    ):
                        inline_images.append(
                            {
                                "filename": attachment_filename,
                                "content": attachment.data,
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
                                "content": attachment.data,
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

        attachments = []
        inline_images = []
        for part in message.walk():
            attachment_filename = part.get_filename()
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
    return header, body, attachments, inline_images


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


def extract_text_from_email(
    email_text: str,
    attachments_data: Sequence[dict[str, Any]],
    *,
    extractor: AttachmentTextExtractor,
    inline_images: Optional[Sequence[dict[str, Any]]] = None,
    manifest: Optional[list[dict[str, Any]]] = None,
    source_prefix: str = "email",
    observed_at: str | None = None,
) -> str:
    records = [source_record(
        source_prefix + ":body", "Email body", email_text.encode("utf-8"),
        source_type="email_body", observed_at=observed_at, text=email_text,
    )]
    attachments = list(attachments_data) + list(inline_images or ())
    results = {}
    if attachments:
        with ThreadPoolExecutor(max_workers=min(getattr(extractor, "max_attachment_workers", 4), len(attachments))) as executor:
            futures = {executor.submit(extract_document, a["content"], a["filename"], extractor): index
                       for index, a in enumerate(attachments)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = (future.result(), "read")
                except AnimatedImageSkipped:
                    results[index] = ("", ANIMATED_IMAGE_SKIP_STATUS)
                except Exception as exc:
                    # Do not place exception messages/URLs into model context.
                    results[index] = ("", "failed:" + type(exc).__name__)
    for index, attachment in enumerate(attachments):
        text, status = results[index]
        records.append(source_record(
            f"{source_prefix}:attachment:{index}", attachment["filename"], attachment["content"],
            source_type="attachment", observed_at=observed_at, text=text, status=status,
        ))
    if manifest is not None:
        manifest.extend(records)
    if any(record["status"] not in COMPLETE_SOURCE_STATUSES for record in records):
        raise IncompleteSourceExtraction(records)
    combined_text = "\n\n".join(
        f"SOURCE ID: {record['sourceId']}\nSOURCE TYPE: {record['sourceType']}\n"
        f"FILE: {record['filename']}\nREQUEST TIMESTAMP: {record['observedAt']}\n"
        + ("EMAIL CONTENT:\n" if record["sourceType"] == "email_body" else "ATTACHMENT CONTENT:\n")
        + record["text"] for record in records if record["status"] == "read"
    )
    if len(combined_text) > MAX_CONTEXT_CHARS:
        raise ValueError("Combined source text exceeds extraction limit; review required")
    return combined_text
