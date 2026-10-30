"""Read and resolve the current holding item and its explicitly linked drawing."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..config import settings
from .. import monday_client
from .design_processing_inputs import parse_design_processing_target
from .legacy_enquiry.formatting import format_date_for_monday
from .performance_specification_contract import FIELD_NAMES, missing


class SpecificationSourceError(ValueError):
    pass


class SpecificationIneligible(SpecificationSourceError):
    pass


def active_specification_group_ids() -> frozenset[str]:
    return frozenset(value.strip() for value in settings.auto_sync_active_group_ids.split(",") if value.strip()) - {
        str(settings.design_processing_landing_group_id), str(settings.auto_sync_completed_group_id),
    }


COLUMN_FIELDS = """
    id type text value column { title }
    ... on DateValue { date }
    ... on DropdownValue { values { id label } }
    ... on BoardRelationValue {
      linked_item_ids linked_items { id name board { id } }
    }
    ... on MirrorValue {
      display_value
      mirrored_items {
        linked_item { id board { id } }
        mirrored_value {
          __typename
          ... on TextValue { text }
          ... on LongTextValue { text }
          ... on NumbersValue { number }
          ... on DateValue { date }
          ... on StatusValue { label }
          ... on DropdownValue { values { id label } }
          ... on BoardRelationValue {
            linked_item_ids linked_items { id name board { id } }
          }
        }
      }
    }
"""
HOLDING_QUERY = """
query ($itemIds: [ID!]) {
  items(ids: $itemIds) {
    id name state board { id } group { id }
    column_values { COLUMN_FIELDS }
  }
}
""".replace("COLUMN_FIELDS", COLUMN_FIELDS)
PROJECT_QUERY = """
query ($itemIds: [ID!]) {
  items(ids: $itemIds) {
    id name state board { id }
    column_values { COLUMN_FIELDS }
    subitems {
      id name state board { id }
      column_values { COLUMN_FIELDS }
    }
  }
}
""".replace("COLUMN_FIELDS", COLUMN_FIELDS)


@dataclass(frozen=True)
class MondaySpecificationGateway:
    access_token: str

    def fetch_target(self, item_id):
        return parse_design_processing_target(monday_client.fetch_design_processing_intake_item(
            self.access_token, item_id,
        ))

    def _read(self, query, item_id):
        payload = monday_client.monday_graphql_request(
            self.access_token, query, {"itemIds": [str(item_id)]}, timeout=30,
        )
        items = (payload.get("data") or {}).get("items")
        if not isinstance(items, list) or len(items) != 1 or str(items[0].get("id")) != str(item_id):
            raise SpecificationSourceError("Specification source item is unavailable")
        return items[0]

    def fetch_holding(self, item_id):
        return self._read(HOLDING_QUERY, item_id)

    def fetch_project(self, item_id):
        return self._read(PROJECT_QUERY, item_id)

    def inspect_files(self, item_id):
        return monday_client.inspect_design_processing_file_columns(self.access_token, item_id)["file_mkza7y37"]

    def upload(self, item_id, filename, content):
        return monday_client.upload_design_file(
            self.access_token, item_id, "file_mkza7y37", filename, content, "application/pdf",
        )

    def delete(self, board_id, item_id, asset_id):
        monday_client.delete_design_file(self.access_token, board_id, item_id, "file_mkza7y37", asset_id)


DRAWING_COLUMN_IDS = {
    "Company": "mirror_12__1", "Contact": "mirror_11__1", "Surveyor": "mirror92__1",
    "Target U Value": "mirror0__1", "Target Min U Value": "mirror12__1",
    "Tapered Insulation": "mirror875__1", "Decking": "mirror75__1",
    "Revision": "mirror_1__1",
}
TITLE_ALIASES = {
    "Target U Value": {"uvalue", "targetuvalue"},
    "Target Min U Value": {"minuvalue", "minimumuvalue", "targetminuvalue"},
    "Tapered Insulation": {"taperedinsulation", "producttype"},
    "Decking": {"decking", "decktype"},
}


def _normal(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _clean(value):
    return None if missing(value) else re.sub(r"\s+", " ", str(value)).strip()


def _columns(item):
    columns = item.get("column_values")
    if not isinstance(columns, list) or any(not isinstance(col, dict) or not col.get("id") for col in columns):
        raise SpecificationSourceError("Incomplete specification columns")
    if len({col["id"] for col in columns}) != len(columns):
        raise SpecificationSourceError("Duplicate specification column IDs")
    return columns


def _relation(col, dependencies):
    linked, ids = col.get("linked_items"), col.get("linked_item_ids")
    if not isinstance(linked, list) or not isinstance(ids, list):
        raise SpecificationSourceError("Incomplete linked specification value")
    if {str(value) for value in ids} != {str(entry.get("id")) for entry in linked}:
        raise SpecificationSourceError("Some linked specification items are unreadable")
    for entry in linked:
        board_id = str((entry.get("board") or {}).get("id") or "")
        if not board_id or not isinstance(entry.get("name"), str):
            raise SpecificationSourceError("Unreadable linked specification identity")
        dependencies.add((board_id, str(entry["id"])))
    return linked


def _typed_value(value, dependencies):
    if "linked_items" in value:
        linked = _relation(value, dependencies)
        if len(linked) > 1:
            raise SpecificationSourceError("A specification field resolves to multiple linked values")
        return _clean(linked[0]["name"]) if linked else None
    if "values" in value:
        options = value["values"]
        if not isinstance(options, list) or any(not isinstance(x.get("label"), str) for x in options):
            raise SpecificationSourceError("Unreadable dropdown specification value")
        labels = {x["label"] for x in options}
        if len(labels) > 1:
            raise SpecificationSourceError("A specification field has multiple dropdown selections")
        return _clean(next(iter(labels))) if labels else None
    for key in ("date", "number", "label", "text"):
        if key in value:
            return _clean(value[key])
    raise SpecificationSourceError("Unsupported or unreadable mirrored specification value")


def _read_column(col, dependencies):
    if col.get("type") == "mirror":
        entries = col.get("mirrored_items")
        if not isinstance(entries, list):
            raise SpecificationSourceError("Incomplete mirrored specification value")
        if not entries and col.get("display_value"):
            raise SpecificationSourceError("Mirrored specification sources have not resolved")
        values = set()
        for entry in entries:
            linked = entry.get("linked_item") or {}
            if not linked.get("id") or not (linked.get("board") or {}).get("id"):
                raise SpecificationSourceError("Incomplete mirrored specification identity")
            dependencies.add((str(linked["board"]["id"]), str(linked["id"])))
            values.add(_typed_value(entry.get("mirrored_value") or {}, dependencies))
        if len(values) > 1:
            raise SpecificationSourceError("Conflicting mirrored specification values")
        return next(iter(values)) if values else None
    if col.get("type") in {"board_relation", "dropdown", "date"}:
        return _typed_value(col, dependencies)
    if "text" not in col:
        raise SpecificationSourceError("Specification column text is unreadable")
    return _clean(col["text"])


def _find_column(item, label, scope):
    columns = _columns(item)
    configured = settings.performance_specification_column_map.get(scope, {})
    column_id = configured.get(label) or (DRAWING_COLUMN_IDS.get(label) if scope == "drawing" else None)
    if column_id:
        matches = [col for col in columns if col["id"] == column_id]
        if matches:
            return matches[0]
        if label in configured:
            raise SpecificationSourceError(f"Configured {scope} column for {label} is missing")
    aliases = TITLE_ALIASES.get(label, {_normal(label)})
    matches = [col for col in columns if _normal((col.get("column") or {}).get("title")) in aliases]
    if len(matches) > 1:
        raise SpecificationSourceError(f"Multiple {scope} columns match {label}; configure an explicit ID")
    return matches[0] if matches else None


def resolve_specification_sources(gateway, item, previous: dict[str, Any] | None = None):
    """Return semantic inputs only; timestamps and unrelated CRM fields are excluded."""
    for scope, mappings in settings.performance_specification_column_map.items():
        if set(mappings) - set(FIELD_NAMES):
            raise SpecificationSourceError(f"Unknown specification field in {scope} column mapping")
    target = gateway.fetch_target(item.item_id)
    if (target.board_id != str(settings.design_processing_board_id)
            or target.item_state != "active" or target.group_id not in active_specification_group_ids()):
        raise SpecificationIneligible("Specification item is outside the active groups")
    extraction = item.extracted_parameters_json or {}
    if (not target.input_revision or extraction.get("inputRevision") != target.input_revision
            or item.latest_analyzed_input_revision != item.latest_desired_input_revision
            or item.latest_analyzed_pipeline_version != item.latest_desired_pipeline_version
            or extraction.get("pipelineVersion") != item.latest_analyzed_pipeline_version):
        raise SpecificationSourceError("Waiting for current email extraction")
    extracted = extraction.get("performanceParameters")
    if not isinstance(extracted, dict) or not extracted:
        raise SpecificationSourceError("Waiting for specification-aware email extraction (output-v8)")
    raw = {label: _clean(extracted.get(label, extracted.get(label.replace("U Value", "U-Value"))))
           for label in FIELD_NAMES}
    raw["Incoming Information"] = ""
    provenance = {label: {"source": "Email Content"} for label in FIELD_NAMES}
    dependencies = set()
    previous = previous or {}

    def apply(label, value, source_item, column_id):
        value = _clean(value)
        source = {"source": "Monday CRM", "boardId": str(source_item["board"]["id"]),
                  "itemId": str(source_item["id"]), "columnId": column_id}
        old_source = (previous.get("sources") or {}).get(label) or {}
        same_source = all(source.get(key) == old_source.get(key) for key in source)
        cleared = value is None and same_source and (
            (previous.get("raw") or {}).get(label) is not None or old_source.get("cleared") is True
        )
        if value is not None or cleared:
            raw[label] = value
            provenance[label] = {**source, "cleared": cleared}

    def apply_columns(source_item, scope):
        for label in FIELD_NAMES:
            if label == "Incoming Information":
                continue
            column = _find_column(source_item, label, scope)
            if column is not None:
                apply(label, _read_column(column, dependencies), source_item, column["id"])

    holding = gateway.fetch_holding(item.item_id)
    if (str((holding.get("board") or {}).get("id")) != target.board_id
            or str(holding.get("id")) != item.item_id or holding.get("state") != "active"
            or (holding.get("group") or {}).get("id") not in active_specification_group_ids()):
        raise SpecificationIneligible("Specification holding item changed scope")
    columns = {col["id"]: col for col in _columns(holding)}
    for required in ("board_relation_mkpbm5np", "board_relation_mm3c4g5x", "date_mkpb23av"):
        if required not in columns:
            raise SpecificationSourceError(f"Holding specification column {required} is missing")
    projects = _relation(columns["board_relation_mkpbm5np"], dependencies)
    if any(str(entry["board"]["id"]) != str(settings.design_processing_project_board_id) for entry in projects):
        raise SpecificationSourceError("TP Ref points to an unexpected project board")
    if len(projects) > 1:
        raise SpecificationSourceError("Select a single TP Ref project for this specification")
    selected_drawing = None
    if projects:
        project = gateway.fetch_project(str(projects[0]["id"]))
        if (str(project.get("id")) != str(projects[0]["id"])
                or str((project.get("board") or {}).get("id")) != str(settings.design_processing_project_board_id)
                or project.get("state") != "active" or not isinstance(project.get("subitems"), list)):
            raise SpecificationSourceError("Linked project is incomplete or inactive")
        apply_columns(project, "project")
        title = next((col for col in _columns(project) if col["id"] == "text3__1"), None)
        if title is not None:
            apply("Drawing Title", _read_column(title, dependencies), project, title["id"])
        drawings = []
        for drawing in project["subitems"]:
            board_id = (drawing.get("board") or {}).get("id")
            if not board_id or not drawing.get("id") or drawing.get("state") not in {"active", "archived", "deleted"}:
                raise SpecificationSourceError("Linked drawing lifecycle is incomplete")
            dependencies.add((str(board_id), str(drawing["id"])))
            if drawing["state"] == "active":
                drawings.append(drawing)
        def reference(value):
            return re.sub(r"\s+", "", str(value or "")).lower().removeprefix("tp")
        requested = reference(raw.get("Drawing Reference"))
        matches = [drawing for drawing in drawings if requested and
                   reference(drawing.get("name")) == requested]
        if len(matches) == 1:
            selected_drawing = matches[0]
        elif len(drawings) == 1 and not requested:
            selected_drawing = drawings[0]
        elif drawings:
            # A newer letter revision of the same drawing may replace an archived
            # request. Never select a different numbered roof/drawing series.
            def series(name):
                match = re.fullmatch(r"(?:TP)?(\d+_\d+\.\d+)\s*-\s*([A-Z]+)", str(name).strip(), re.IGNORECASE)
                return (match[1].lower(), match[2].upper()) if match else None
            wanted = series(raw.get("Drawing Reference"))
            same_series = []
            for drawing in drawings:
                candidate = series(drawing.get("name"))
                if (wanted and candidate and candidate[0] == wanted[0]
                        and (len(candidate[1]), candidate[1]) > (len(wanted[1]), wanted[1])):
                    same_series.append(drawing)
            if len(same_series) == 1:
                selected_drawing = same_series[0]
            else:
                raise SpecificationSourceError("Requested drawing/roof is missing or ambiguous")
        if selected_drawing is not None:
            name = str(selected_drawing.get("name") or "")
            if re.match(r"^(?:TP)?\d+_", name, re.IGNORECASE):
                apply("Drawing Reference", name, selected_drawing, "name")
                apply("Revision", name.split("_", 1)[1].strip(), selected_drawing, "name")
            apply_columns(selected_drawing, "drawing")

    apply_columns(holding, "holding")
    accounts = _relation(columns["board_relation_mm3c4g5x"], dependencies)
    if len(accounts) > 1:
        raise SpecificationSourceError("Several Accounts exist; specification Company is ambiguous")
    apply("Company", accounts[0]["name"] if accounts else None, holding, "board_relation_mm3c4g5x")
    apply("Date Received", _read_column(columns["date_mkpb23av"], dependencies), holding, "date_mkpb23av")
    if raw.get("Date Received"):
        formatted = format_date_for_monday(raw["Date Received"])
        if not formatted:
            raise SpecificationSourceError("Date Received is not a supported date")
        raw["Date Received"] = formatted
    return {
        "raw": raw, "sources": provenance,
        "dependencies": [list(value) for value in sorted(dependencies)],
        "emailRevision": target.input_revision,
        "extractionPipeline": extraction["pipelineVersion"],
        "drawingItemId": str(selected_drawing["id"]) if selected_drawing else None,
    }
