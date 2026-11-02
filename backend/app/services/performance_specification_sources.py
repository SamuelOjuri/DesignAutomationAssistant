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
from .performance_specification_evidence import (
    PAGE_SIZE_CONFLICT, PAGE_SIZE_UNCERTAIN, REVIEW_PREFIX_REFERENCE, UNDATED_SCOPE_CONFLICT, VALUE_CONFLICT,
    resolve_baseline_drawing_identity, resolve_email_evidence, resolve_request_notes,
)
from .specification_semantics import (
    drawing_reference_key, normalize_reference_dashes, review_prefix_reference, tp_reference_revision,
)


class SpecificationSourceError(ValueError):
    pass


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
UNMATCHED_REVIEW_PREFIX = (
    "Drawing Reference prefix is not TP and matches no single linked drawing. "
    "Confirm the drawing reference; drawing-specific CRM values have not been used"
)


class SpecificationReviewRequired(SpecificationSourceError):
    def __init__(self, source, issues):
        self.source, self.issues = source, issues
        super().__init__("Specification needs review: " + "; ".join(f"{field}: {reason}" for field, reason in issues.items()))


def active_specification_group_ids() -> frozenset[str]:
    return frozenset(value.strip() for value in settings.auto_sync_active_group_ids.split(",") if value.strip()) - {
        str(settings.design_processing_landing_group_id), str(settings.auto_sync_completed_group_id),
    }


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
    if extraction.get("pipelineVersion") != settings.design_processing_pipeline_version:
        raise SpecificationSourceError("Waiting for extraction from the current configured pipeline")
    if (not target.input_revision or extraction.get("inputRevision") != target.input_revision
            or item.latest_analyzed_input_revision != item.latest_desired_input_revision
            or item.latest_analyzed_pipeline_version != item.latest_desired_pipeline_version
            or extraction.get("pipelineVersion") != item.latest_analyzed_pipeline_version):
        raise SpecificationSourceError("Waiting for current email extraction")
    extracted = extraction.get("performanceParameters")
    if not isinstance(extracted, dict) or not extracted:
        raise SpecificationSourceError("Waiting for evidence-aware email extraction (output-v9)")
    try:
        raw, provenance, issues = resolve_email_evidence(extraction)
        request_notes = resolve_request_notes(extraction)
    except ValueError as exc:
        raise SpecificationSourceError(str(exc)) from exc
    dependencies = set()
    crm_reads = []
    selected_drawing = None
    drawing_selection = None

    def snapshot():
        from .specification_request_context import request_context_summary, select_client_request
        return {
            "raw": raw, "sources": provenance, "crmReads": crm_reads,
            "sourceManifest": extraction["sourceManifest"],
            "requestContext": request_context_summary(select_client_request(extraction["sourceManifest"])),
            "requestNotes": request_notes,
            "evidenceRepairs": extraction.get("specificationEvidenceRepairs", []),
            "evidenceVersion": extraction["specificationEvidenceVersion"], "issues": issues,
            "dependencies": [list(value) for value in sorted(dependencies)],
            "emailRevision": target.input_revision, "extractionPipeline": extraction["pipelineVersion"],
            "drawingItemId": str(selected_drawing["id"]) if selected_drawing else None,
            **({"drawingSelection": drawing_selection} if drawing_selection else {}),
        }

    def require_review(label, reason):
        issues[label] = reason
        raise SpecificationReviewRequired(snapshot(), issues)

    def apply(label, value, source_item, column_id):
        value = _clean(value)
        source = {"source": "Monday CRM", "boardId": str(source_item["board"]["id"]),
                  "itemId": str(source_item["id"]), "columnId": column_id}
        crm_reads.append({**source, "field": label, "value": value, "status": "empty" if value is None else "provided"})
        if value is not None:
            raw[label] = value
            provenance[label] = {**source, "status": "provided", "rationale": "Current populated CRM value takes precedence"}
            issues.pop(label, None)

    def apply_columns(source_item, scope, labels=FIELD_NAMES):
        for label in labels:
            if label == "Incoming Information":
                continue
            column = _find_column(source_item, label, scope)
            if column is not None:
                try:
                    value = _read_column(column, dependencies, gateway)
                except SpecificationValueConflict as exc:
                    if exc.values is None or label not in {"Tapered Insulation", "Membrane", "VCL", "Decking"}:
                        require_review(label, str(exc))
                    source = {"source": "Monday CRM", "boardId": str(source_item["board"]["id"]),
                              "itemId": str(source_item["id"]), "columnId": column["id"]}
                    raw[label] = None
                    issues[label] = MULTI_MATERIAL
                    provenance[label] = {**source, "status": "conflict",
                        "candidates": [{"value": value, "scope": None} for value in exc.values],
                        "columnValue": {key: val for key, val in column.items() if key not in {"value", "display_value"}}}
                    crm_reads.append({**source, "field": label, "values": exc.values, "status": "conflict"})
                    continue
                apply(label, value, source_item, column["id"])
                if value is not None:
                    provenance[label]["columnValue"] = {key: value for key, value in column.items()
                                                        if key not in {"value", "display_value"}}

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
        require_review("Drawing Reference", "Select a single TP Ref project for this specification")
    if projects:
        project = gateway.fetch_project(str(projects[0]["id"]))
        if (str(project.get("id")) != str(projects[0]["id"])
                or str((project.get("board") or {}).get("id")) != str(settings.design_processing_project_board_id)
                or project.get("state") != "active" or not isinstance(project.get("subitems"), list)):
            raise SpecificationSourceError("Linked project is incomplete or inactive")
        # Resolve identity before reading project rollups or drawing-specific
        # fields. An unassigned request must not inherit another roof's specs.
        apply_columns(project, "project", ("Drawing Reference",))
        apply_columns(holding, "holding", ("Drawing Reference",))
        drawings = []
        for drawing in project["subitems"]:
            board_id = (drawing.get("board") or {}).get("id")
            if not board_id or not drawing.get("id") or drawing.get("state") not in {"active", "archived", "deleted"}:
                raise SpecificationSourceError("Linked drawing lifecycle is incomplete")
            dependencies.add((str(board_id), str(drawing["id"])))
            if drawing["state"] == "active":
                drawings.append(drawing)
        def reference(value):
            return drawing_reference_key(value)
        requested_value = raw.get("Drawing Reference")
        if not requested_value:
            if "Drawing Reference" in issues:
                require_review("Drawing Reference", issues["Drawing Reference"])
            identity_candidates, identity_issue = resolve_baseline_drawing_identity(extraction)
            # A review-prefix identity still selects its CRM drawing; it is flagged below.
            if identity_issue and identity_issue != REVIEW_PREFIX_REFERENCE:
                require_review("Drawing Reference", identity_issue)
            if identity_candidates:
                requested_value = identity_candidates[0]["value"]
                drawing_selection = {
                    "status": "baseline_reference", "reference": requested_value,
                    "evidence": identity_candidates,
                }
        drawing_identities = [
            {"boardId": str(drawing["board"]["id"]), "itemId": str(drawing["id"]),
             "reference": str(drawing.get("name") or "")}
            for drawing in sorted(drawings, key=lambda drawing: str(drawing["id"]))
        ]

        def unmatched_review_prefix():
            # Another two-letter prefix publishes for review without a CRM drawing;
            # an unmatched TP/AX, D.S or bare reference still blocks.
            if not review_prefix_reference(requested_value):
                require_review("Drawing Reference", "Requested drawing/roof is missing or ambiguous")
            return {**(drawing_selection or {}), "status": "unmatched_review_prefix",
                    "reason": UNMATCHED_REVIEW_PREFIX, "reference": requested_value,
                    "candidates": drawing_identities}

        requested = reference(requested_value)
        matches = [drawing for drawing in drawings if requested and
                   reference(drawing.get("name")) == requested]
        if len(matches) == 1:
            selected_drawing = matches[0]
        elif len(drawings) == 1 and not requested:
            selected_drawing = drawings[0]
        elif drawings and not requested:
            drawing_selection = {
                "status": "unassigned", "reason": UNASSIGNED_DRAWING,
                "candidates": drawing_identities,
            }
        elif drawings:
            # A newer letter revision of the same drawing may replace an archived
            # request. Never select a different numbered roof/drawing series.
            def series(name):
                match = re.fullmatch(r"((?:[a-z]{2}|d\.s-)?\d+_\d+\.\d+)-([a-z]+)", reference(name))
                return (match[1].lower(), match[2].upper()) if match else None
            wanted = series(requested_value)
            same_series = []
            for drawing in drawings:
                candidate = series(drawing.get("name"))
                if (wanted and candidate and candidate[0] == wanted[0]
                        and (len(candidate[1]), candidate[1]) > (len(wanted[1]), wanted[1])):
                    same_series.append(drawing)
            if len(same_series) == 1:
                selected_drawing = same_series[0]
            else:
                drawing_selection = unmatched_review_prefix()
        elif drawing_selection and drawing_selection["status"] == "baseline_reference":
            drawing_selection = unmatched_review_prefix()
        if drawing_selection is None or drawing_selection["status"] not in {"unassigned", "unmatched_review_prefix"}:
            apply_columns(project, "project", tuple(label for label in FIELD_NAMES if label != "Drawing Reference"))
        title = next((col for col in _columns(project) if col["id"] == "text3__1"), None)
        if title is not None:
            apply("Drawing Title", _read_column(title, dependencies, gateway), project, title["id"])
        if selected_drawing is not None:
            name = str(selected_drawing.get("name") or "")
            if tp_reference_revision(name, confirmed_crm=True, review_prefix=True):
                apply("Drawing Reference", name, selected_drawing, "name")
            apply_columns(selected_drawing, "drawing")

    apply_columns(holding, "holding", tuple(label for label in FIELD_NAMES if label != "Drawing Reference")
                  if projects else FIELD_NAMES)
    accounts = _relation(columns["board_relation_mm3c4g5x"], dependencies)
    if len(accounts) > 1:
        require_review("Company", "Several Accounts exist; specification Company is ambiguous")
    apply("Company", accounts[0]["name"] if accounts else None, holding, "board_relation_mm3c4g5x")
    apply("Date Received", _read_column(columns["date_mkpb23av"], dependencies, gateway), holding, "date_mkpb23av")
    # Re-evaluate attachment candidates with the now-confirmed CRM target. Only
    # fallback fields change here; current non-empty CRM values keep precedence.
    fallback, evidence_sources, scoped_issues = resolve_email_evidence(
        extraction, (raw.get("Bauder Roof Num"), raw.get("Drawing Reference")),
    )
    for label in FIELD_NAMES:
        if provenance[label].get("source") != "Monday CRM":
            raw[label], provenance[label] = fallback[label], evidence_sources[label]
            issues.pop(label, None)
            if label in scoped_issues:
                issues[label] = scoped_issues[label]
    if drawing_selection and drawing_selection["status"] == "unassigned":
        issues["Drawing Reference"] = UNASSIGNED_DRAWING
        provenance["Drawing Reference"]["status"] = "conflict"
    elif drawing_selection and drawing_selection["status"] == "unmatched_review_prefix":
        if raw.get("Drawing Reference") is None:
            # A baseline identity is not request evidence; it is published only for review.
            raw["Drawing Reference"] = normalize_reference_dashes(drawing_selection["reference"])
            provenance["Drawing Reference"] = {
                "source": "Email/Attachment", "selected": None,
                "candidates": drawing_selection.get("evidence", []),
                "rationale": "Existing drawing identity; no single linked drawing matches",
            }
        if issues.get("Drawing Reference") in {None, REVIEW_PREFIX_REFERENCE}:
            issues["Drawing Reference"] = UNMATCHED_REVIEW_PREFIX
        provenance["Drawing Reference"]["status"] = "conflict"
    confirmed_reference = provenance["Drawing Reference"].get("source") == "Monday CRM" or any(
        e.get("issuer") == "TaperedPlus" for e in provenance["Drawing Reference"].get("candidates", [])
    )
    suffix = tp_reference_revision(raw.get("Drawing Reference"), confirmed_crm=confirmed_reference, review_prefix=True)
    if raw.get("Drawing Reference") and suffix is None:
        issues["Drawing Reference"] = "Reference is not a verified TaperedPlus drawing"
    if review_prefix_reference(raw.get("Drawing Reference")):
        # Accepted, including from CRM, but never published as a verified TP drawing.
        issues.setdefault("Drawing Reference", REVIEW_PREFIX_REFERENCE)
    if suffix:
        rejected_revisions = provenance["Revision"].get("rejectedCandidates", [])
        revision_value = raw.get("Revision")
        if provenance["Revision"].get("source") != "Monday CRM" and raw.get("Drawing Reference") != fallback.get("Drawing Reference"):
            revision_value = None  # An older requested drawing does not define the replacement's revision.
        # A compatible letter mirror is retained as provenance but cannot replace
        # the full TP reference suffix. A conflicting suffix/letter requires review.
        if revision_value and revision_value.casefold() not in {suffix.casefold(), suffix.rsplit("-", 1)[-1].strip().casefold()}:
            issues["Revision"] = "Revision conflicts with the selected TaperedPlus drawing reference"
        elif not any(e["status"] == "unverified" for e in rejected_revisions):
            issues.pop("Revision", None)
        raw["Revision"] = suffix
        provenance["Revision"] = {"source": "Derived", "status": "provided", "from": "Drawing Reference",
                                  "referenceSource": provenance["Drawing Reference"], "observedRevision": revision_value,
                                  **({"rejectedCandidates": rejected_revisions} if rejected_revisions else {})}
    elif raw.get("Revision"):
        issues["Revision"] = "Revision requires a verified TaperedPlus Drawing Reference"
    if raw.get("Date Received"):
        formatted = format_date_for_monday(raw["Date Received"])
        if not formatted:
            raise SpecificationSourceError("Date Received is not a supported date")
        raw["Date Received"] = formatted
    source = snapshot()
    # Fully read, verified alternatives can be published as field review notes.
    # Unverified evidence and incomplete/ambiguous CRM reads still fail the refresh.
    publishable_conflicts = {
        VALUE_CONFLICT, UNDATED_SCOPE_CONFLICT, PAGE_SIZE_CONFLICT, PAGE_SIZE_UNCERTAIN, MULTI_MATERIAL,
        UNASSIGNED_DRAWING, REVIEW_PREFIX_REFERENCE, UNMATCHED_REVIEW_PREFIX,
        "Revision conflicts with the selected TaperedPlus drawing reference",
    }
    if any(reason not in publishable_conflicts
           or label not in extraction.get("specificationAssessedFields", [])
           or any(candidate["status"] == "unverified"
                  for candidate in provenance[label].get("rejectedCandidates", []))
           for label, reason in issues.items()):
        raise SpecificationReviewRequired(source, issues)
    return source
