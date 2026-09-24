"""Run independently of document ingestion: python -m backend.app.services.monday_metadata_worker."""

import argparse
import json
import logging
import time

from ..db import SessionLocal
from .auto_sync import get_monday_ingestion_access_token
from .monday_metadata import run_metadata_once
from .monday_metadata_fields import COLUMN_IDS, normalize_fields
from ..monday_client import MONDAY_METADATA_QUERY, fetch_monday_metadata

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh current Monday CRM context without document processing")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--inspect-item", help="Read and validate one Monday item without writing to the database")
    parser.add_argument("--print-query", action="store_true", help="Print a Postman JSON body; use --inspect-item for its item ID")
    args = parser.parse_args()
    if args.limit < 1 or args.poll_seconds <= 0:
        parser.error("limit and poll-seconds must be positive")
    if args.print_query:
        print(json.dumps({"query": MONDAY_METADATA_QUERY, "variables": {
            "itemIds": [args.inspect_item or "REPLACE_WITH_ITEM_ID"], "columnIds": sorted(COLUMN_IDS),
        }}, indent=2))
        return 0
    if args.inspect_item:
        try:
            fields = normalize_fields(fetch_monday_metadata(get_monday_ingestion_access_token(), args.inspect_item))
            print(json.dumps({"itemId": args.inspect_item, "fields": fields}, indent=2))
        except Exception as exc:
            print(f"Monday metadata inspection failed ({type(exc).__name__}): {getattr(exc, 'detail', str(exc))}")
            return 1
        return 0
    while True:
        try:
            with SessionLocal() as db:
                results = run_metadata_once(db, get_monday_ingestion_access_token(), limit=args.limit)
                logger.info("Monday metadata batch results=%s", results)
        except Exception:
            logger.exception("Monday metadata worker batch failed")
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
