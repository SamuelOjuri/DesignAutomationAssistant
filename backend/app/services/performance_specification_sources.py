"""Read-only Monday source access and candidate preparation for specifications."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..config import settings
from .. import monday_client
from .design_processing_inputs import parse_design_processing_target
from .monday_metadata_fields import PROJECT_NAME_COLUMN_ID
from .performance_specification_contract import missing
from .specification_references import REFERENCE_PARSER_VERSION, recommend_drawing_candidates


class SpecificationSourceError(ValueError):
    pass


class SpecificationSourceWaiting(SpecificationSourceError):
    """Current input/extraction is not ready; a prior PDF is no longer current."""


class SpecificationIneligible(SpecificationSourceError):
    pass


class SpecificationValueConflict(SpecificationSourceError):
    def __init__(self, message, *, values=None):
        super().__init__(message)
        self.values = values


MULTI_MATERIAL = "Multiple CRM material selections require a scoped specification"
UNASSIGNED_DRAWING = (
    "No requested TaperedPlus drawing reference identifies a single linked drawing. "
    "Confirm the drawing reference; drawing-specific CRM values have not been used"
)
UNMATCHED_DRAWING = (
    "Requested drawing matches no single linked active drawing. "
    "Confirm the drawing reference; drawing-specific CRM values have not been used"
)


class SpecificationReviewRequired(SpecificationSourceError):
    def __init__(self, source, issues):
        self.source, self.issues = source, issues
        super().__init__("Specification needs review: " + "; ".join(f"{field}: {reason}" for field, reason in issues.items()))


COLUMN_FIELDS = """
    id type text value column { title }
    ... on DateValue { date }
    ... on DropdownValue { values { id label } }
    ... on PeopleValue { persons_and_teams { id kind } }
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
          ... on PeopleValue { text persons_and_teams { id kind } }
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

    def resolve_person(self, kind, entity_id):
        root = {"person": "users", "team": "teams"}.get(kind)
        if root is None:
            raise SpecificationSourceError("Unsupported People identity kind")
        query = f"query ($ids: [ID!]) {{ {root}(ids: $ids) {{ id name }} }}"
        payload = monday_client.monday_graphql_request(self.access_token, query, {"ids": [str(entity_id)]}, timeout=30)
        entries = (payload.get("data") or {}).get(root)
        if not isinstance(entries, list) or len(entries) != 1 or str(entries[0].get("id")) != str(entity_id) or not entries[0].get("name"):
            raise SpecificationSourceError("People identity is unavailable")
        return entries[0]["name"]

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


def _project_display_labels(candidate=None, project=None):
    """Use available project metadata without changing its selection identity."""
    candidate, project = candidate or {}, project or {}
    columns = project.get("column_values")
    title_columns = [
        column for column in columns
        if isinstance(column, dict) and column.get("id") == PROJECT_NAME_COLUMN_ID
    ] if isinstance(columns, list) else []
    title = _clean(title_columns[0].get("text")) if len(title_columns) == 1 else None
    return {
        "project_reference": _clean(candidate.get("projectReference")) or _clean(project.get("name"))
            or _clean(candidate.get("resolvedProjectReference")),
        "project_title": _clean(candidate.get("projectTitle")) or title,
    }


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
    for entry in linked:
        board_id = str((entry.get("board") or {}).get("id") or "")
        if not board_id or not isinstance(entry.get("name"), str):
            raise SpecificationSourceError("Unreadable linked specification identity")
        dependencies.add((board_id, str(entry["id"])))
    if {str(value) for value in ids} != {str(entry.get("id")) for entry in linked}:
        raise SpecificationSourceError("Some linked specification items are unreadable")
    return linked


def _typed_value(value, dependencies, gateway=None):
    if "persons_and_teams" in value or value.get("__typename") == "PeopleValue" or value.get("type") == "people":
        entities = value.get("persons_and_teams")
        if not isinstance(entities, list):
            raise SpecificationSourceError("Incomplete People specification value")
        if len(entities) > 1:
            raise SpecificationValueConflict("A specification field has multiple People identities")
        if not entities:
            if value.get("text"):
                raise SpecificationSourceError("People identities have not resolved")
            return None
        entity = entities[0]
        if not entity.get("id") or entity.get("kind") not in {"person", "team"} or gateway is None:
            raise SpecificationSourceError("Unreadable People specification identity")
        name = gateway.resolve_person(entity["kind"], str(entity["id"]))
        value["resolvedPeople"] = [{"id": str(entity["id"]), "kind": entity["kind"], "name": name}]
        return _clean(name)
    if "linked_items" in value:
        linked = _relation(value, dependencies)
        if len(linked) > 1:
            raise SpecificationValueConflict("A specification field resolves to multiple linked values")
        return _clean(linked[0]["name"]) if linked else None
    if "values" in value:
        options = value["values"]
        if not isinstance(options, list) or any(not isinstance(x, dict) or not isinstance(x.get("label"), str) or not x["label"].strip() for x in options):
            raise SpecificationSourceError("Unreadable dropdown specification value")
        labels = {x["label"] for x in options}
        if len(labels) > 1:
            raise SpecificationValueConflict("A specification field has multiple dropdown selections", values=sorted(labels))
        return _clean(next(iter(labels))) if labels else None
    for key in ("date", "number", "label", "text"):
        if key in value:
            return _clean(value[key])
    raise SpecificationSourceError("Unsupported or unreadable mirrored specification value")


def _read_column(col, dependencies, gateway=None):
    if col.get("type") == "mirror":
        entries = col.get("mirrored_items")
        if not isinstance(entries, list):
            raise SpecificationSourceError("Incomplete mirrored specification value")
        if not entries and col.get("display_value"):
            raise SpecificationSourceError("Mirrored specification sources have not resolved")
        values = set()
        multiple = False
        for entry in entries:
            linked = entry.get("linked_item") or {}
            if not linked.get("id") or not (linked.get("board") or {}).get("id"):
                raise SpecificationSourceError("Incomplete mirrored specification identity")
            dependencies.add((str(linked["board"]["id"]), str(linked["id"])))
        # Preserve every known mirror origin even if an earlier value cannot be
        # read. A later source correction must be able to wake this review.
        for entry in entries:
            try:
                values.add(_typed_value(entry.get("mirrored_value") or {}, dependencies, gateway))
            except SpecificationValueConflict as exc:
                if exc.values is None:
                    raise
                multiple = True
                values.update(exc.values)
        if multiple:
            if None in values:
                raise SpecificationSourceError("Some mirrored material selections are unresolved")
            raise SpecificationValueConflict("Multiple mirrored material selections", values=sorted(values))
        if len(values) > 1:
            raise SpecificationValueConflict("Conflicting mirrored specification values")
        return next(iter(values)) if values else None
    if col.get("type") in {"board_relation", "dropdown", "date", "people"}:
        return _typed_value(col, dependencies, gateway)
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
    if scope == "project" and label == "Contact":
        # Project Contact commonly rolls up mirrors from every drawing/request.
        # Only an explicit mapping above may opt into that scope. Otherwise use
        # the selected drawing's Contact, holding override or verified evidence.
        return None
    aliases = TITLE_ALIASES.get(label, {_normal(label)})
    matches = [col for col in columns if _normal((col.get("column") or {}).get("title")) in aliases]
    if len(matches) > 1:
        raise SpecificationSourceError(f"Multiple {scope} columns match {label}; configure an explicit ID")
    return matches[0] if matches else None


def prepare_specification_candidates(gateway, match_contract, intent, *, project_board_id, scope=None):
    """Read candidate context without selecting a project or merging PDF values.

    This sidecar works before a holding TP Ref/Accounts/date exists. Reads are
    bounded; a broad title search remains visible and asks for narrowing rather
    than pretending a partial drawing inventory is complete.
    """
    from .specification_review_contract import CandidateLookup, DrawingCandidate, ProjectMatch
    from .design_processing_target import DesignProcessingTargetMismatch
    from .specification_references import parse_drawing_reference, parse_project_reference

    projects = []
    for candidate in match_contract.get("candidates", []):
        projects.append(ProjectMatch(
            record={"board_id": str(project_board_id), "item_id": str(candidate["mondayItemId"])},
            rationale=candidate.get("matchRationale") or "Project title similarity; supporting context only",
            similarity_score=candidate.get("similarity"),
            **_project_display_labels(candidate),
        ))
    lookup = CandidateLookup(status="available", projects=projects)
    dependencies = {(str(project_board_id), project.record.item_id) for project in projects}
    selections, accounts = [], []
    reader = getattr(gateway, "fetch_specification_project", None)
    if projects and reader is None:
        lookup.status = "pending"
        lookup.issues.append("Project/drawing details have not been read")
    if len(projects) > 12:
        lookup.status = "pending"
        lookup.issues.append("Narrow the project candidates to read further drawing details")
    references = (intent or {}).get("detected_references", [])
    requested = [entry for entry in references if entry.get("kind") == "drawing"]
    inactive_projects = [entry for entry in (match_contract.get("referenceLookup") or {}).get("candidates", [])
                         if entry.get("state") in {"archived", "deleted"}]
    if inactive_projects:
        lookup.issues.append("A detected project reference is archived or deleted; select an active project or review the request")
    for candidate in match_contract.get("candidates", [])[:12] if reader else []:
        item_id = str(candidate["mondayItemId"])
        try:
            project = reader(item_id)
            if (str(project.get("id")) != item_id
                    or str((project.get("board") or {}).get("id")) != str(project_board_id)
                    or project.get("state") != "active" or not isinstance(project.get("subitems"), list)):
                raise SpecificationSourceError("Project details are incomplete or inactive")
            matched_project = next(entry for entry in lookup.projects if entry.record.item_id == item_id)
            for label, value in _project_display_labels(project=project).items():
                if not getattr(matched_project, label) and value:
                    setattr(matched_project, label, value)
            project_reference = candidate.get("resolvedProjectReference") or project.get("name")
            printed = parse_project_reference(project_reference)
            relevant = [entry for entry in requested
                        if (parsed := parse_drawing_reference(entry["literal"]))
                        and printed and parsed.project_key == printed.key]
            if relevant:
                priority = {"current_request": 0, "document": 1, "forwarded_context": 2}
                best_role = min(priority.get(entry.get("role", "current_request"), 2) for entry in relevant)
                relevant = list(dict.fromkeys(entry["literal"] for entry in relevant
                    if priority.get(entry.get("role", "current_request"), 2) == best_role))
            ranked = recommend_drawing_candidates(
                project["subitems"], project_reference=project_reference, scope=scope,
                requested_reference=relevant[0] if len(relevant) == 1 else None,
            )
            if len(relevant) > 1:
                ranked["selectedItemId"] = None
                ranked["status"] = "ambiguous"
                ranked["issues"].append("Several email drawing references need reviewer selection")
            selections.append({"projectItemId": item_id, **ranked})
            lookup.issues.extend(ranked["issues"])
            recommended_ids = {entry["itemId"] for entry in ranked["recommendations"]}
            for drawing in ranked["candidates"]:
                if not drawing["boardId"] or not drawing["itemId"]:
                    continue
                dependencies.add((drawing["boardId"], drawing["itemId"]))
                lookup.drawings.append(DrawingCandidate.model_validate({
                    "record": {"board_id": drawing["boardId"], "item_id": drawing["itemId"]},
                    "project": {"board_id": str(project_board_id), "item_id": item_id},
                    "literal_reference": drawing["reference"] or "Unreadable reference",
                    "roof_scope": drawing["scope"],
                    "lifecycle": drawing["state"] if drawing["state"] in {"active", "archived", "deleted"} else "unknown",
                    "match_kind": "exact_email" if ranked["status"] == "exact" and drawing["itemId"] == ranked["selectedItemId"]
                        else "latest_in_scope" if drawing["itemId"] in recommended_ids else "other",
                    "rationale": "Exact relevant email drawing" if ranked["status"] == "exact" and drawing["itemId"] == ranked["selectedItemId"]
                        else "Existing drawing candidate; confirm project and roof scope",
                }))
            account = _find_column(project, "Company", "project")
            if account is None:
                account_columns = [column for column in _columns(project)
                    if _normal((column.get("column") or {}).get("title")) in {"account", "accounts"}]
                if len(account_columns) > 1:
                    raise SpecificationSourceError("Several project Account columns require an explicit mapping")
                account = account_columns[0] if account_columns else None
            if account is not None:
                accounts.append({"projectItemId": item_id, "boardId": str(project_board_id),
                    "columnId": account["id"], "value": _read_column(account, dependencies, gateway)})
        except DesignProcessingTargetMismatch:
            raise
        except Exception:
            # Optional CRM suggestions cannot destroy durable email extraction.
            # Errors deliberately contain no upstream bodies, tokens or email text.
            lookup.status = "unavailable"
            lookup.issues.append(f"Project {item_id} recommendations are unavailable; retry the lookup")
    if any((match_contract.get(key) or {}).get("status") == "unavailable"
           for key in ("referenceLookup", "titleLookup")):
        lookup.status = "unavailable"
        lookup.issues.append("Some project candidates could not be read; retry before selecting")
    # Validate after assembling: never persist a partially typed source snapshot.
    lookup = CandidateLookup.model_validate(lookup.model_dump())
    return {"schemaVersion": 1, "referenceParserVersion": REFERENCE_PARSER_VERSION,
        "lookup": lookup.model_dump(), "drawingSelections": selections,
        "inactiveProjectMatches": inactive_projects,
        "accountRecommendations": accounts,
        "dependencies": [list(value) for value in sorted(dependencies)]}


def resolve_specification_sources(gateway, item, previous: dict[str, Any] | None = None, *, review_state=None):
    """Resolve email-first recommendations and durable PDF-only decisions."""
    from .specification_review_sources import resolve_review_sources
    return resolve_review_sources(gateway, item, previous, review_state=review_state)
