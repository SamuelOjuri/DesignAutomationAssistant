"""Board-specific CRM field contract. No network or database side effects."""

from copy import deepcopy
import hashlib
import json
from typing import Any

FIELDS = (
    ("accounts", "board_relation_mm3c4g5x", "Accounts", "board_relation"),
    ("enquiryType", "dropdown_mkpb98es", "New Enq / Amend", "dropdown"),
    ("tpRef", "board_relation_mkpbm5np", "TP Ref", "board_relation"),
    ("projectName", "lookup_mkpb44am", "Project Name", "mirror"),
    ("zipCode", "dropdown_mkpbafca", "Zip Code", "dropdown"),
)
COLUMN_IDS = frozenset(field[1] for field in FIELDS)
COLUMN_TITLES = frozenset(field[2] for field in FIELDS)
ACCOUNTS_BOARD_ID = "1654217230"
PROJECT_BOARD_ID = "1825117125"
PROJECT_NAME_COLUMN_ID = "text3__1"
LINKED_BOARD_IDS = frozenset({ACCOUNTS_BOARD_ID, PROJECT_BOARD_ID})


class MetadataReadError(ValueError):
    """An incomplete read must never be interpreted as a user clearing a field."""


def normalize_fields(item: dict[str, Any]) -> list[dict[str, Any]]:
    columns = {col.get("id"): col for col in item.get("column_values", [])}
    result = []
    for key, column_id, title, kind in FIELDS:
        col = columns.get(column_id)
        if not col or col.get("type") != kind:
            raise MetadataReadError(f"Missing or unexpected Monday column: {column_id}")
        values = []
        linked_items = []
        if kind == "board_relation":
            ids, linked = col.get("linked_item_ids"), col.get("linked_items")
            if not isinstance(ids, list) or not isinstance(linked, list):
                raise MetadataReadError(f"Missing linked values for {title}")
            expected_board = ACCOUNTS_BOARD_ID if key == "accounts" else PROJECT_BOARD_ID
            for entry in linked:
                board_id = str((entry.get("board") or {}).get("id") or "")
                if not entry.get("id") or not isinstance(entry.get("name"), str) or board_id != expected_board:
                    raise MetadataReadError(f"Unreadable or unexpected linked item for {title}")
                linked_items.append({"id": str(entry["id"]), "name": entry["name"], "boardId": board_id})
            if {str(value) for value in ids} != {entry["id"] for entry in linked_items}:
                raise MetadataReadError(f"Some linked items are unavailable for {title}")
            linked_items.sort(key=lambda entry: entry["id"])
            values = [{"id": entry["id"], "label": entry["name"]} for entry in linked_items]
        elif kind == "dropdown":
            options = col.get("values")
            if not isinstance(options, list):
                raise MetadataReadError(f"Missing dropdown values for {title}")
            for option in options:
                if option.get("id") is None or not isinstance(option.get("label"), str):
                    raise MetadataReadError(f"Invalid dropdown value for {title}")
                values.append({"id": str(option["id"]), "label": option["label"]})
            values.sort(key=lambda entry: entry["id"])
        else:
            # Keep each project's identity; commas in a project name are not separators.
            mirrored = col.get("mirrored_items")
            if not isinstance(mirrored, list):
                raise MetadataReadError("Missing Project Name mirror values")
            for entry in mirrored:
                source = entry.get("mirrored_value") or {}
                linked_id = (entry.get("linked_item") or {}).get("id")
                if linked_id is None or "text" not in source or source["text"] is not None and not isinstance(source["text"], str):
                    raise MetadataReadError("Unreadable Project Name source")
                values.append({"id": str(linked_id), "label": source["text"] or ""})
            values.sort(key=lambda entry: entry["id"])
        display = ", ".join(entry["label"] for entry in values if entry["label"])
        result.append({
            "key": key, "columnId": column_id, "title": title, "type": kind,
            "displayValue": display, "values": values, "linkedItems": linked_items,
            "state": "set" if display else "empty", "source": "Monday CRM",
        })
    project_ids = {entry["id"] for entry in result[2]["linkedItems"]}
    if project_ids != {entry["id"] for entry in result[3]["values"]}:
        raise MetadataReadError("Project Name mirror has not resolved all linked projects")
    return result


def metadata_revision(fields: list[dict[str, Any]]) -> str:
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def only_crm_fields_changed(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Only skip document work for an observed CRM edit with identical other inputs."""
    def comparable(item: dict[str, Any]) -> dict[str, Any]:
        def assets(items):
            return sorted([
                {key: asset.get(key) for key in ("id", "name", "file_extension", "file_size", "created_at")}
                for asset in items
            ], key=lambda asset: str(asset["id"]))
        return {
            "name": item.get("name"),
            "assets": assets(item.get("assets") or []),
            "update_assets": assets([asset for update in item.get("updates") or [] for asset in update.get("assets") or []]),
            "columns": sorted([
                deepcopy(col) for col in item.get("column_values") or [] if col.get("id") not in COLUMN_IDS
            ], key=lambda col: str(col.get("id"))),
        }
    before = [col for col in previous.get("column_values") or [] if col.get("id") in COLUMN_IDS]
    after = [col for col in current.get("column_values") or [] if col.get("id") in COLUMN_IDS]
    return before != after and comparable(previous) == comparable(current)
