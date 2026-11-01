from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import hashlib
import io
import re
from typing import Any, Optional, Protocol, Sequence, Union
from zoneinfo import ZoneInfo

import extract_msg

from .source_documents import (
    IncompleteSourceExtraction, MAX_CONTEXT_CHARS,
    extract_source_records, html_text, source_record,
)
from ..specification_semantics import COMPLETE_SOURCE_STATUSES
from ..specification_request_context import attachment_message, parent_email, request_context_summary
from ..source_text import clean_source_text


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
        if source_metadata is not None:
            html_part = message.get_body(preferencelist=("html",))
            if html_part is not None:
                source_metadata.update(_html_source_metadata(header, html_part.get_content()))

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
) -> str:
    records = [source_record(
        source_prefix + ":body", "Email body", email_text.encode("utf-8"),
        source_type="email_body", observed_at=observed_at, text=email_text,
    )]
    records[0].update(source_metadata or {})
    attachments = list(attachments_data) + list(inline_images or ())
    results = {}
    if attachments:
        with ThreadPoolExecutor(max_workers=min(getattr(extractor, "max_attachment_workers", 4), len(attachments))) as executor:
            futures = {executor.submit(extract_source_records, a["content"], a["filename"], extractor,
                                       source_id=f"{source_prefix}:attachment:{index}", observed_at=observed_at): index
                       for index, a in enumerate(attachments)}
            for future in as_completed(futures):
                index = futures[future]
                results[index] = future.result()
    for index in range(len(attachments)):
        content_id = attachments[index].get("content_id")
        if content_id:
            results[index][0]["contentId"] = str(content_id).strip("<>")
        records.extend(results[index])
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
    combined_text = "\n\n".join(
        f"SOURCE ID: {record['sourceId']}\nSOURCE TYPE: {record['sourceType']}\n"
        f"FILE: {record['filename']}\nCARRYING EMAIL TIMESTAMP (not the fact's date): {record['observedAt']}\n"
        + (f"CONTENT-ID: {record['contentId']}\n" if record.get("contentId") else "")
        + (f"VERIFIED CONTAINING MESSAGE DATE: {record['containingMessage']['sentDate']}\n"
           if record.get("containingMessage") else "")
        + ("EMAIL CONTENT:\n" if record["sourceType"] == "email_body" else "ATTACHMENT CONTENT:\n")
        + record["text"]
        + ("\nHTML ALTERNATIVE OF THE SAME EMAIL (literal text and inline Content-IDs):\n"
           + record["emailHtmlText"] if record.get("emailHtmlText") else "")
        for record in records if record["status"] == "read"
    )
    if len(combined_text) > MAX_CONTEXT_CHARS:
        raise ValueError("Combined source text exceeds extraction limit; review required")
    return combined_text
