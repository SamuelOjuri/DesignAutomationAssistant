"""Local, read-only source email inventory; no model or remote calls."""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.services.legacy_enquiry.email_extraction import process_email_content
from backend.app.services.specification_request_context import email_messages

output = ROOT / "tmp" / "request-note-3345815639"
output.mkdir(exist_ok=True)
results = []
for label in ("a", "b", "c"):
    path = Path(r"C:\Users\SamuelOjuri\Downloads") / f"3345815639 - {label}.eml"
    content = path.read_bytes()
    metadata = {}
    header, body, attachments, images = process_email_content(content, path.name, source_metadata=metadata)
    text = header + "\n" + body
    (output / f"{label}-body.txt").write_text(text, encoding="utf-8")
    (output / f"{label}-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    result = {"label": label, "sha256": hashlib.sha256(content).hexdigest(), "header": header,
              "body_chars": len(body), "body_excerpt": body[:9500],
              "attachments": [{"filename": part["filename"], "bytes": len(part["content"] or b""),
                               "sha256": hashlib.sha256(part["content"] or b"").hexdigest()}
                              for part in attachments],
              "inline_images": [{"filename": part["filename"], "bytes": len(part["content"] or b"")}
                                for part in images],
              "messages": [{key: value for key, value in message.items() if key != "text"}
                           for message in email_messages(text)]}
    results.append(result)
    print(json.dumps(result, ensure_ascii=True, indent=2))
(output / "inventory.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
