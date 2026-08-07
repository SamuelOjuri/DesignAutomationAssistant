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
                body = message.body
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
        body = ""
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    body += part.get_content() + "\n"
        else:
            body = message.get_content()

        attachments = []
        inline_images = []
        for part in message.iter_attachments():
            attachment_filename = part.get_filename()
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
) -> str:
    combined_text = f"EMAIL CONTENT:\n{email_text}\n\n"
    pdf_attachments = sorted(
        [
            attachment
            for attachment in attachments_data
            if attachment["filename"].lower().endswith(".pdf")
        ],
        key=lambda attachment: len(attachment["content"]),
    )
    visual_items: list[tuple[str, Any]] = []
    if pdf_attachments and extractor.should_batch_pdfs(pdf_attachments):
        visual_items.append(("pdf_batch", pdf_attachments))
    else:
        visual_items.extend(("pdf", pdf) for pdf in pdf_attachments)

    image_attachments = [
        attachment
        for attachment in attachments_data
        if any(
            attachment["filename"].lower().endswith(extension)
            for extension in (".jpg", ".jpeg", ".png", ".gif", ".bmp")
        )
    ]
    visual_items.extend(("image", image) for image in image_attachments)
    visual_items.extend(("inline", image) for image in (inline_images or ()))

    non_visual = [
        attachment
        for attachment in attachments_data
        if not any(
            attachment["filename"].lower().endswith(extension)
            for extension in (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".bmp")
        )
    ]
    for attachment in non_visual:
        combined_text += (
            f"\nATTACHMENT ({attachment['filename']}) "
            "[Not processed - not a PDF or image]\n\n"
        )

    def process_visual(item_type: str, item: Any) -> tuple[str, str]:
        if item_type == "pdf_batch":
            text = extractor.process_pdf_batch(item)
            return "batched_pdfs", f"\nBATCHED PDF ATTACHMENTS:\n{text}\n\n"
        if item_type == "pdf":
            text = extractor.process_pdf(item["content"], item["filename"])
            return item["filename"], f"\nPDF ATTACHMENT ({item['filename']}):\n{text}\n\n"
        if item_type == "inline":
            text = extractor.process_image(
                item["content"],
                item["filename"],
                "INLINE IMAGE",
            )
            return item["filename"], f"\nINLINE IMAGE ({item['filename']}):\n{text}\n\n"
        text = extractor.process_image(
            item["content"],
            item["filename"],
            "ATTACHMENT",
        )
        return item["filename"], f"\nIMAGE ATTACHMENT ({item['filename']}):\n{text}\n\n"

    results: list[tuple[str, str]] = []
    if visual_items:
        with ThreadPoolExecutor(
            max_workers=min(extractor.max_attachment_workers, len(visual_items)),
        ) as executor:
            future_items = {
                executor.submit(process_visual, item_type, item): (item_type, item)
                for item_type, item in visual_items
            }
            for future in as_completed(future_items):
                item_type, item = future_items[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    filename = (
                        f"batch_of_{len(item)}_items"
                        if isinstance(item, list)
                        else item.get("filename", "unknown")
                    )
                    results.append((filename, f"Error processing {filename}: {exc}"))

        order = {
            (
                "batched_pdfs"
                if item_type == "pdf_batch"
                else item["filename"]
            ): index
            for index, (item_type, item) in enumerate(visual_items)
        }
        for _, text in sorted(results, key=lambda result: order.get(result[0], 999999)):
            combined_text += text
    return combined_text
