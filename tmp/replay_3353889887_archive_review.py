"""Offline oversized ZIP referral; no expansion, model calls or production writes."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.services.legacy_enquiry.source_documents import (
    MAX_SOURCE_BYTES, oversized_archive_file_record, permanent_source_failure,
)
from backend.app.services.performance_specification_contract import source_review_notes
from backend.app.services.specification_semantics import OVERSIZED_ARCHIVE_REVIEW_STATUS

path = Path(r"C:\Users\SamuelOjuri\Downloads\wetransfer_13056-albermarle-music-centre_2026-11-10_1027.zip")
record = oversized_archive_file_record(path, path.name, source_id="asset:295860667")
assert MAX_SOURCE_BYTES == 40 * 1024 * 1024
assert record["sizeBytes"] == 515317032
assert record["contentSha256"] == "d24a6a471da1e5c8447b814853d321336c39badefac1570030977cffdb1dc36c"
assert record["status"] == OVERSIZED_ARCHIVE_REVIEW_STATUS and record["text"] == ""
assert not permanent_source_failure([record])
recommendation, = source_review_notes([record])
assert "Download and review the original ZIP manually" in recommendation
print(json.dumps({"status": record["status"], "size_bytes": record["sizeBytes"],
                  "original_hash_verified": True, "zip_limit_bytes": MAX_SOURCE_BYTES,
                  "archive_opened": False, "recommendation": recommendation}, ensure_ascii=True))
