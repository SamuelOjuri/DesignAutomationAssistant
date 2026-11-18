"""Print the Monday board inventory for the read-only specification diagnostic.

Run with `python -m backend.scripts.export_specification_live_items` in the worker
environment. The UTC snapshot timestamp marks the start of pagination.
"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from fastapi import HTTPException

from backend.app.config import settings
from backend.app.monday_client import MondayReadContractError, fetch_live_board_group_items


SNAPSHOT_QUOTE = "$monday_snapshot$"
SNAPSHOT_PLACEHOLDER = f"{SNAPSHOT_QUOTE}{{}}{SNAPSHOT_QUOTE}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sql", action="store_true", help="Print the diagnostic SQL with the snapshot filled in.")
    args = parser.parse_args()
    template = None
    if args.sql:
        template_path = Path(__file__).with_name("diagnose_performance_specifications.sql")
        template = template_path.read_text(encoding="utf-8")
        if template.count(SNAPSHOT_PLACEHOLDER) != 1:
            parser.exit(1, "The diagnostic SQL must have one Monday snapshot placeholder.\n")

    token = settings.monday_ingestion_access_token
    if not token:
        parser.exit(1, "MONDAY_INGESTION_ACCESS_TOKEN is not configured.\n")

    board_id = str(settings.design_processing_board_id)
    observed_at = datetime.now(timezone.utc).isoformat()
    try:
        items = fetch_live_board_group_items(token, board_id)
    except MondayReadContractError as exc:
        parser.exit(1, f"{exc.detail}. No snapshot was exported.\n")
    except HTTPException as exc:
        parser.exit(1, f"Monday inventory read failed (HTTP {exc.status_code}). No snapshot was exported.\n")
    snapshot = {"board_id": board_id, "observed_at": observed_at, "items": [asdict(item) for item in items]}
    # Keep the JSON safe to paste inside the diagnostic's PostgreSQL dollar quotes.
    output = json.dumps(snapshot, separators=(",", ":")).replace("$", r"\u0024")
    if template is not None:
        output = template.replace(SNAPSHOT_PLACEHOLDER, f"{SNAPSHOT_QUOTE}{output}{SNAPSHOT_QUOTE}", 1)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
