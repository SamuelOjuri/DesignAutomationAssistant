import io
import logging
import time
import tempfile
import os

from email.parser import BytesParser
from email import policy
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from typing import Tuple, Dict, List, Union
import extract_msg

from .pdf_extraction import process_pdf_with_gemini, process_pdf_batch, should_batch_pdfs
from .image_extraction import process_image_with_gemini
from .thread_pool import process_items_in_parallel

logger = logging.getLogger(__name__)

def process_email_content(email_content: bytes, filename: str) -> Tuple[str, str, List[Dict], List[Dict]]:
    if filename.lower().endswith(".msg"):
        with io.BytesIO(email_content) as bio:
            msg = extract_msg.Message(bio)
            try:
                raw_date = msg.date or ""
                local_date_str = format_email_date(raw_date)
                header_info = f"From: {msg.sender}\nTo: {msg.to}\nSubject: {msg.subject}\nDate: {local_date_str}\n"
                body = msg.body or ""
                attachments_data, inline_images = [], []
                for attachment in msg.attachments:
                    att_filename = attachment.longFilename or attachment.shortFilename
                    if not att_filename:
                        continue
                    if is_inline_attachment(attachment, msg, att_filename):
                        inline_images.append({
                            "filename": att_filename,
                            "content": attachment.data,
                            "content_id": getattr(attachment, "cid", None),
                            "mime_type": f"image/{att_filename.split('.')[-1].lower()}",
                        })
                    else:
                        attachments_data.append({"filename": att_filename, "content": attachment.data})
            finally:
                msg.close()
    else:
        msg = BytesParser(policy=policy.default).parsebytes(email_content)
        raw_date = msg.get("date", "")
        local_date_str = format_email_date(raw_date)
        header_info = f"From: {msg.get('from','')}\nTo: {msg.get('to','')}\nSubject: {msg.get('subject','')}\nDate: {local_date_str}\n"
        body = ""

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    body += part.get_content() + "\n"
            if not body:
                for part in msg.walk():
                    if part.get_content_type() == "text/html" and not part.get_filename():
                        body += part.get_content() + "\n"
        else:
            body = msg.get_content()

        attachments_data, inline_images = [], []
        for part in msg.iter_attachments():
            att_filename = part.get_filename()
            if not att_filename:
                continue
            content = part.get_payload(decode=True)
            if is_inline_image(part, att_filename):
                inline_images.append({
                    "filename": att_filename,
                    "content": content,
                    "content_id": part.get("Content-ID"),
                    "mime_type": part.get_content_type(),
                })
            else:
                attachments_data.append({"filename": att_filename, "content": content})

    return header_info, body, attachments_data, inline_images

def process_email_content_to_temp(
    email_content: bytes, filename: str
) -> Tuple[str, str, List[Dict], List[Dict]]:
    """
    Memory-efficient version that writes attachments to temp files.
    Returns attachment metadata with temp_path instead of content bytes.
    """
    if filename.lower().endswith(".msg"):
        with io.BytesIO(email_content) as bio:
            msg = extract_msg.Message(bio)
            try:
                raw_date = msg.date or ""
                local_date_str = format_email_date(raw_date)
                header_info = f"From: {msg.sender}\nTo: {msg.to}\nSubject: {msg.subject}\nDate: {local_date_str}\n"
                body = msg.body or ""
                attachments_data, inline_images = [], []
                for attachment in msg.attachments:
                    att_filename = attachment.longFilename or attachment.shortFilename
                    if not att_filename:
                        continue
                    # Write to temp file instead of holding in memory
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{att_filename}")
                    tmp.write(attachment.data)
                    tmp.close()
                    
                    if is_inline_attachment(attachment, msg, att_filename):
                        inline_images.append({
                            "filename": att_filename,
                            "temp_path": tmp.name,
                            "content_id": getattr(attachment, "cid", None),
                            "mime_type": f"image/{att_filename.split('.')[-1].lower()}",
                        })
                    else:
                        attachments_data.append({"filename": att_filename, "temp_path": tmp.name})
            finally:
                msg.close()
    else:
        msg = BytesParser(policy=policy.default).parsebytes(email_content)
        raw_date = msg.get("date", "")
        local_date_str = format_email_date(raw_date)
        header_info = f"From: {msg.get('from','')}\nTo: {msg.get('to','')}\nSubject: {msg.get('subject','')}\nDate: {local_date_str}\n"
        body = ""

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    body += part.get_content() + "\n"
            if not body:
                for part in msg.walk():
                    if part.get_content_type() == "text/html" and not part.get_filename():
                        body += part.get_content() + "\n"
        else:
            body = msg.get_content()

        attachments_data, inline_images = [], []
        for part in msg.iter_attachments():
            att_filename = part.get_filename()
            if not att_filename:
                continue
            content = part.get_payload(decode=True)
            
            # Write to temp file instead of holding in memory
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{att_filename}")
            tmp.write(content)
            tmp.close()
            del content  # Free memory immediately
            
            if is_inline_image(part, att_filename):
                inline_images.append({
                    "filename": att_filename,
                    "temp_path": tmp.name,
                    "content_id": part.get("Content-ID"),
                    "mime_type": part.get_content_type(),
                })
            else:
                attachments_data.append({"filename": att_filename, "temp_path": tmp.name})

    return header_info, body, attachments_data, inline_images


def cleanup_temp_files(attachments: List[Dict], inline_images: List[Dict] = None):
    """Clean up any remaining temp files."""
    for att in attachments:
        if "temp_path" in att:
            try:
                os.unlink(att["temp_path"])
            except OSError:
                pass
    for img in (inline_images or []):
        if "temp_path" in img:
            try:
                os.unlink(img["temp_path"])
            except OSError:
                pass

def format_email_date(raw_date: Union[str, object]) -> str:
    try:
        dt = parsedate_to_datetime(raw_date) if isinstance(raw_date, str) else raw_date
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo("Europe/London")).strftime("%a, %d %b %Y %H:%M:%S %z")
    except Exception:
        return str(raw_date)

def is_inline_image(part, filename: str) -> bool:
    return filename.lower().endswith((".jpg",".jpeg",".png",".gif",".bmp")) and bool(part.get("Content-ID"))

def is_inline_attachment(attachment, msg, filename: str) -> bool:
    return filename.lower().endswith((".jpg",".jpeg",".png",".gif",".bmp")) and (
        (hasattr(attachment, "cid") and attachment.cid) or
        (hasattr(msg, "htmlBody") and msg.htmlBody and filename in msg.htmlBody.decode("utf-8", errors="ignore"))
    )

def _split_batched_pdf_text(text: str) -> Dict[str, str]:
    by_file: Dict[str, str] = {}
    current_name: str | None = None
    buffer: List[str] = []

    for line in text.splitlines():
        if line.startswith("=== PDF:"):
            if current_name is not None:
                by_file[current_name] = "\n".join(buffer).strip()
            current_name = line.replace("=== PDF:", "").replace("===", "").strip()
            buffer = []
        else:
            buffer.append(line)

    if current_name is not None:
        by_file[current_name] = "\n".join(buffer).strip()

    return by_file


def extract_email_sections(
    header: str,
    body: str,
    attachments_data: List[Dict],
    inline_images: List[Dict] | None = None,
) -> List[Dict[str, str]]:
    sections: List[Dict[str, str]] = []

    if header.strip():
        sections.append({"section": "email:header", "text": header})
    if body.strip():
        sections.append({"section": "email:body", "text": body})

    inline_images = inline_images or []

    pdf_attachments = sorted(
        [att for att in attachments_data if att["filename"].lower().endswith(".pdf")],
        key=lambda x: len(x["content"]),
    )
    image_attachments = [
        att for att in attachments_data
        if any(att["filename"].lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp"))
    ]
    non_visual = [
        att for att in attachments_data
        if not any(att["filename"].lower().endswith(ext) for ext in (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".bmp"))
    ]

    for attachment in non_visual:
        filename = attachment["filename"]
        sections.append(
            {
                "section": f"email:attachment:{filename}",
                "text": f"ATTACHMENT ({filename}) [Not processed]",
            }
        )

    # PDFs
    if pdf_attachments and should_batch_pdfs(pdf_attachments):
        batched_text = process_pdf_batch(pdf_attachments)
        by_file = _split_batched_pdf_text(batched_text)
        for att in pdf_attachments:
            filename = att["filename"]
            text = by_file.get(filename, "").strip()
            if text:
                text = f"PDF ATTACHMENT ({filename}):\n{text}"
            else:
                text = f"PDF ATTACHMENT ({filename}) [Not processed]"
            sections.append(
                {"section": f"email:attachment:{filename}", "text": text}
            )
    else:
        visual_items = []
        visual_items.extend(("pdf", pdf) for pdf in pdf_attachments)
        visual_items.extend(("image", img) for img in image_attachments)
        visual_items.extend(("inline", img) for img in inline_images)

        def _process_visual(item_type, item):
            filename = item["filename"]
            if item_type == "pdf":
                text = process_pdf_with_gemini(item["content"], filename)
                return filename, f"PDF ATTACHMENT ({filename}):\n{text}"
            if item_type == "inline":
                text = process_image_with_gemini(item["content"], filename, "INLINE IMAGE")
                return filename, f"INLINE IMAGE ({filename}):\n{text}"
            text = process_image_with_gemini(item["content"], filename, "ATTACHMENT")
            return filename, f"IMAGE ATTACHMENT ({filename}):\n{text}"

        results = process_items_in_parallel(visual_items, _process_visual, max_workers=15)
        order_map = {}
        for idx, item in enumerate(visual_items):
            order_map[item[1]["filename"]] = idx

        for filename, text in sorted(results, key=lambda x: order_map.get(x[0], 999999)):
            sections.append(
                {"section": f"email:attachment:{filename}", "text": text}
            )

    return sections

# def extract_text_from_email(email_text: str, attachments_data: List[Dict], inline_images: List[Dict] = None) -> str:
#     combined_text = f"EMAIL CONTENT:\n{email_text}\n\n"

#     pdf_attachments = sorted(
#         [att for att in attachments_data if att["filename"].lower().endswith(".pdf")],
#         key=lambda x: len(x["content"])
#     )
#     visual_items = []
#     if pdf_attachments and should_batch_pdfs(pdf_attachments):
#         visual_items.append(("pdf_batch", pdf_attachments))
#     else:
#         visual_items.extend(("pdf", pdf) for pdf in pdf_attachments)

#     image_attachments = [
#         att for att in attachments_data
#         if any(att["filename"].lower().endswith(ext) for ext in (".jpg",".jpeg",".png",".gif",".bmp"))
#     ]
#     visual_items.extend(("image", img) for img in image_attachments)
#     if inline_images:
#         visual_items.extend(("inline", img) for img in inline_images)

#     non_visual = [
#         att for att in attachments_data
#         if not any(att["filename"].lower().endswith(ext) for ext in (".pdf",".jpg",".jpeg",".png",".gif",".bmp"))
#     ]
#     for attachment in non_visual:
#         combined_text += f"\nATTACHMENT ({attachment['filename']}) [Not processed]\n\n"

#     def _process_visual(item_type, item):
#         if item_type == "pdf_batch":
#             text = process_pdf_batch(item)
#             return "batched_pdfs", f"\nBATCHED PDF ATTACHMENTS:\n{text}\n\n"
#         if item_type == "pdf":
#             text = process_pdf_with_gemini(item["content"], item["filename"])
#             return item["filename"], f"\nPDF ATTACHMENT ({item['filename']}):\n{text}\n\n"
#         if item_type == "inline":
#             text = process_image_with_gemini(item["content"], item["filename"], "INLINE IMAGE")
#             return item["filename"], f"\nINLINE IMAGE ({item['filename']}):\n{text}\n\n"
#         text = process_image_with_gemini(item["content"], item["filename"], "ATTACHMENT")
#         return item["filename"], f"\nIMAGE ATTACHMENT ({item['filename']}):\n{text}\n\n"

#     results = process_items_in_parallel(visual_items, _process_visual, max_workers=15)
#     order_map = {}
#     for idx, item in enumerate(visual_items):
#         order_map["batched_pdfs" if item[0]=="pdf_batch" else item[1]["filename"]] = idx
#     for _, text in sorted(results, key=lambda x: order_map.get(x[0], 999999)):
#         combined_text += text

#     return combined_text