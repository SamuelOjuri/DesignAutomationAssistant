"""Inspect literal request selection and compatible intent; no model or network."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.config import settings
from backend.app.services.legacy_enquiry.email_extraction import process_email_content
from backend.app.services.legacy_enquiry.source_documents import source_record
from backend.app.services.specification_request_context import (
    email_messages, request_context_summary, select_client_request, source_email_messages,
)
from backend.app.services.specification_intent import build_request_intent

EMAIL = Path(r"C:\Users\SamuelOjuri\Downloads\20261111-115947-bb164505116e4fc9b738c257c3bd9e20.eml")
metadata = {}
header, body, attachments, inline_images = process_email_content(
    EMAIL.read_bytes(), EMAIL.name, source_metadata=metadata,
)
text = header + "\n" + body
source = source_record("asset:1:body", "Email body", text.encode("utf-8"),
                       source_type="email_body", text=text)
source.update(metadata)
manifest = [source]
context = select_client_request(manifest)
result = {
    "internal_domains": settings.design_processing_internal_email_domains,
    "selected_client_request": context,
    "fallback_intent": build_request_intent(manifest).model_dump(mode="json"),
    "messages": [{**request_context_summary(message), "text": message["text"]}
                 for message in source_email_messages(source)],
    "plain_text_messages": [{**request_context_summary(message), "text": message["text"]}
                            for message in email_messages(text)],
    "plain_text_only_selection": select_client_request([{key: value for key, value in source.items()
                                                          if key not in {"emailHtmlText", "emailOuterFooterText"}}]),
    "email_attachment_names": [item["filename"] for item in attachments],
    "inline_image_names": [item["filename"] for item in inline_images],
}
OUT = Path(__file__).with_suffix(".json")
OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps({
    "selected_client_request": context,
    "fallback_intent": result["fallback_intent"],
    "plain_text_only_selection": result["plain_text_only_selection"],
    "plain_text_messages": [{**request_context_summary(message), "text_excerpt": message["text"][:1800]}
                            for message in email_messages(text)],
    "messages": [{**request_context_summary(message), "text_excerpt": message["text"][:1800]}
                 for message in source_email_messages(source)],
    "output_path": str(OUT),
}, ensure_ascii=True, indent=2))
