"""Validate source provenance and resolve field candidates without merging roofs."""

from __future__ import annotations

from collections import defaultdict
from datetime import timezone
from email.utils import getaddresses
import re
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse, parse as parse_datetime

from .specification_semantics import (
    COMPLETE_SOURCE_STATUSES, EVIDENCE_VERSION, FIELD_DEFINITIONS, REFERENCE_FIELDS, U_VALUE_FIELDS,
    SpecificationEvidence, SpecificationRequestNote, drawing_reference_key, missing,
    normalize_reference_dashes, normalize_unit_spacing, scale_ratio, tp_reference_revision,
)
from .specification_request_context import (
    CLIENT_FIELDS, attachment_message, client_evidence_applies,
    parent_email, select_client_request, source_email_messages,
)
from .specification_image_text import literal_source_texts

FIELD_NAMES = tuple(FIELD_DEFINITIONS)


VALUE_CONFLICT = "Conflicting roof/drawing/request values require a confirmed target"
UNDATED_SCOPE_CONFLICT = "Undated project-wide value conflicts with the selected roof"
PAGE_SIZE_CONFLICT = "Title-block paper size differs from the PDF page dimensions; confirm the intended drawing size"
PAGE_SIZE_UNCERTAIN = "Ambiguous title-block paper size requires confirmation"
# Extraction cannot read Monday CRM. The publisher confirms such a literal D.S/bare
# reference against a CRM drawing or replaces this with a review-required issue.
UNCONFIRMED_REFERENCE = "Drawing Reference requires Monday CRM confirmation as a TaperedPlus drawing"


def _parse_message_date(value):
    # dayfirst=True changes YYYY-MM-DD dates with a day <= 12. ISO timestamps
    # in source metadata must keep their explicit year-month-day order.
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:T|\s|$)", value):
        return isoparse(value)
    return parse_datetime(value, dayfirst=True)


def _normal(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _excerpt_normal(value):
    # PDF text readers may add Markdown emphasis/code around labels or cells.
    # Ignore only paired presentation markers, preserving numbers, punctuation,
    # table separators and word order. Never join excerpts across an ellipsis.
    text = re.sub(r"\\([*`])", r"\1", str(value or ""))
    text = re.sub(r"(?<!\*)\*\*([^*\n]+)\*\*(?!\*)|`([^`\n]+)`", lambda m: m[1] or m[2], text)
    text = re.sub(r"(?<=\d)\s*:\s*(?=\d)", ":", text)
    return _normal(text)


def _contains_excerpt(excerpt, text):
    excerpt, text = _excerpt_normal(excerpt), _excerpt_normal(text)
    if not excerpt:
        return False
    # A short value such as A1 or 1:10 cannot match the prefix of A10 or 1:100.
    start = r"(?<!\w)" if re.match(r"\w", excerpt[0]) else ""
    end = r"(?!\w)" if re.match(r"\w", excerpt[-1]) else ""
    return re.search(start + re.escape(excerpt) + end, text) is not None


def _source_contains_excerpt(source, excerpt):
    return any(_contains_excerpt(excerpt, text) for text in literal_source_texts(source))


def _reference_normal(value):
    return _normal(normalize_reference_dashes(value))


def _comparable_value(field, value):
    """Presentation-only differences per field; numbers, words and order are unchanged."""
    if field in REFERENCE_FIELDS:
        return normalize_reference_dashes(value)
    if field in U_VALUE_FIELDS:
        return normalize_unit_spacing(value)
    return value


def _contains_reference(value, snippet):
    # The snippet itself is verified literally against its source first; only
    # the drawing-reference value may read a typographic dash as '-'.
    return _contains_excerpt(normalize_reference_dashes(value), normalize_reference_dashes(snippet))


def _candidate_key(evidence):
    # A repaired excerpt may replace a rejected one only for the same claim.
    # Different values, sources, scopes, roles or dates still require review.
    # A verified literal 1/50 also settles a reformatted 1:50 for that claim.
    def part(key):
        if key == "source_id":
            return evidence.get(key)
        if key == "value" and evidence.get("field") == "Scale" and scale_ratio(evidence.get(key)):
            return scale_ratio(evidence.get(key))
        if key == "value":
            return _normal(_comparable_value(evidence.get("field"), evidence.get(key)))
        return _normal(evidence.get(key))
    return tuple(part(key) for key in (
        "field", "value", "source_id", "scope", "issuer", "role", "request_role", "message_date",
    ))


def _title_block_reference_parts(snippet):
    """Return one literal drawing number and one labelled revision from a block.

    Never combine numbers from different drawings or select among revision rows.
    The caller still verifies the entire contiguous block against its source.
    """
    text = normalize_reference_dashes(_excerpt_normal(snippet))
    numbers = re.findall(
        r"(?<!\w)(?:(?:TP|AX)\s*|D\.S\s*-\s*)?\d+_\d+\.\d+(?:\s*-\s*[A-Z]+)?(?!\w|\.\w)",
        text, re.IGNORECASE,
    )
    revisions = re.findall(r"\brev(?:ision)?[\s:|=.-]*([A-Z]+)(?!\w)", text, re.IGNORECASE)
    if len(numbers) != 1 or len(revisions) != 1:
        return None
    suffix = tp_reference_revision(numbers[0], confirmed_crm=True)
    if not suffix or not re.fullmatch(r"\d+\.\d+", suffix):
        return None  # An already combined reference cannot acquire another letter.
    return numbers[0], revisions[0]


def _value_supported(evidence):
    value, snippet = evidence["value"], evidence["snippet"]
    if evidence["field"] == "Contact":
        headers = re.findall(r"(?mi)^From:[ \t]*([^\r\n]+)", snippet)
        if len(headers) == 1:
            addresses = getaddresses(headers)
            if len(addresses) == 1 and "@" in addresses[0][1]:
                parts = addresses[0][0].split(",")
                if len(parts) == 2 and all(part.strip() for part in parts):
                    # Outlook's literal 'Surname, Given name' display name.
                    # Reorder whole parts only; no initials or inferred names.
                    if _normal(value) == _normal(parts[1] + " " + parts[0]):
                        return True
    if evidence.get("reference_parts"):
        parts = evidence["reference_parts"]
        verified = _title_block_reference_parts(snippet)
        if evidence["field"] not in REFERENCE_FIELDS or len(parts) != 2:
            return False
        # Some responses redundantly split an already literal reference at '_'.
        # The full identifier must still occur contiguously in this same excerpt.
        literal = parts[0] + "_" + parts[1]
        suffix = tp_reference_revision(literal, confirmed_crm=True)
        if suffix and _contains_reference(literal, snippet):
            expected = literal if evidence["field"] == "Drawing Reference" else suffix
            if _reference_normal(value) == _reference_normal(expected):
                return True
        combined = parts[0] + " - " + parts[1]
        separate_cells = verified is not None and tuple(map(_reference_normal, parts)) == verified
        # Some responses redundantly split an already contiguous reference.
        # Accept only that exact reference, with an unrevised number and letter;
        # never assemble pieces from unrelated passages.
        literal_reference = (
            re.fullmatch(r"\d+\.\d+", tp_reference_revision(parts[0], confirmed_crm=True) or "")
            and re.fullmatch(r"[A-Za-z]+", parts[1]) and _contains_reference(combined, snippet)
        )
        if not (separate_cells or literal_reference):
            return False
        expected = combined if evidence["field"] == "Drawing Reference" else tp_reference_revision(combined, confirmed_crm=True)
        return _reference_normal(value) == _reference_normal(expected)
    if evidence["field"] == "Revision":
        # A suffix follows '_' inside an identifier, where a word-boundary
        # substring check would otherwise reject a perfectly literal revision.
        for match in re.finditer(r"(?<!\w)(?:(?:TP|AX)\s*|D\.S\s*-\s*)?\d+_\d+\.\d+(?:\s*-\s*[A-Z]+)?(?!\w|\.\w)",
                                 normalize_reference_dashes(snippet), re.IGNORECASE):
            if _reference_normal(tp_reference_revision(match[0], confirmed_crm=True)) == _reference_normal(value):
                return True
        parts = _title_block_reference_parts(snippet)
        if parts:
            suffix = tp_reference_revision(parts[0] + " - " + parts[1], confirmed_crm=True)
            if _reference_normal(value) == _reference_normal(suffix):
                return True
    if evidence["field"] in REFERENCE_FIELDS:
        return _contains_reference(value, snippet)
    if evidence["field"] in U_VALUE_FIELDS:
        return _contains_excerpt(normalize_unit_spacing(value), normalize_unit_spacing(snippet))
    return evidence["field"] == "Date Received" or _contains_excerpt(value, snippet)


def _equivalent_description(shorter, longer):
    """Only duplicate descriptions of the SAME source passage, scope and request.

    This is not a product alias map. Both values must already be literal and the
    shorter description must occur as complete words inside the longer one.
    """
    return (shorter["field"] in {"Membrane", "VCL", "Tapered Insulation"}
            and all(shorter.get(k) == longer.get(k) for k in
                    ("source_id", "scope", "role", "request_role", "message_date", "issuer"))
            and _excerpt_normal(shorter["snippet"]) == _excerpt_normal(longer["snippet"])
            and _contains_excerpt(shorter["value"], longer["value"]))


def _request_note_issue(note, sources):
    source = sources.get(note["source_id"])
    if not source or source["status"] != "read" or not _source_contains_excerpt(source, note["snippet"]):
        return "Request note has an unverified source/excerpt"
    if note["scope"] and not _source_contains_excerpt(source, note["scope"]):
        return "Request note has an unverified roof scope"
    return None


def request_note_diagnostics(extraction):
    """Safe, indexed reasons for the same bounded recovery used by CAD fields."""
    sources = {entry["sourceId"]: entry for entry in extraction.get("sourceManifest", [])}
    issues = {}
    for index, value in enumerate(extraction.get("specificationRequestNotes") or []):
        note = SpecificationRequestNote.model_validate(value).model_dump()
        if reason := _request_note_issue(note, sources):
            issues[index] = reason
    return issues


def resolve_request_notes(extraction):
    sources = {entry["sourceId"]: entry for entry in extraction.get("sourceManifest", [])}
    notes = []
    for index, value in enumerate(extraction.get("specificationRequestNotes") or []):
        note = SpecificationRequestNote.model_validate(value).model_dump()
        if reason := _request_note_issue(note, sources):
            raise ValueError(f"{reason} (note index {index})")
        source = sources.get(note["source_id"])
        note.update(filename=source["filename"], contentSha256=source["contentSha256"], observedAt=source.get("observedAt"))
        if note not in notes:
            notes.append(note)
    return notes


def _has_parent_request_date(evidence, source, sources):
    """Verify a request date through its own parent and literal message headers."""
    source_id = source["sourceId"]
    if source.get("contentId"):
        parent = parent_email(source, sources)
        if not parent or source.get("observedAt") != parent.get("observedAt"):
            return False
        if evidence["field"] in {"Company", "Contact"}:
            context = select_client_request(list(sources.values()))
            if (context and _normal(context["sentDate"]) == _normal(evidence["message_date"])
                    and client_evidence_applies(evidence, source, sources, context, _contains_excerpt)):
                return True  # A repeated client signature still identifies that client.
        message = attachment_message(source, sources)
        return (message is not None and _normal(message["sentDate"]) == _normal(evidence["message_date"])
                and evidence["request_role"] in {message["requestRole"], "document"})
    if (source.get("sourceType") != "attachment" or ":attachment:" not in source_id
            or evidence["request_role"] == "forwarded_request" or not source.get("observedAt")):
        return False
    parent = sources.get(source_id.split(":attachment:", 1)[0] + ":body")
    if (not parent or parent.get("sourceType") != "email_body" or parent["status"] != "read"
            or parent.get("observedAt") != source["observedAt"]):
        return False
    # The reader puts the outer headers before the first blank line. Dates in
    # forwarded bodies or unrelated emails cannot date an attachment candidate.
    header = re.split(r"\r?\n[ \t]*\r?\n", parent["text"], maxsplit=1)[0]
    dates = re.findall(r"^Date:[ \t]*([^\r\n]+)", header, flags=re.MULTILINE)
    return len(dates) == 1 and _normal(dates[0]) == _normal(evidence["message_date"])


def _scope_key(value):
    value = _normal(value)
    value = re.sub(r"^(?:bauder\s+)?roof\s*(?:num(?:ber)?\s*)?[:#-]?\s*", "", value)
    value = re.sub(r"\s+", "", value)
    return drawing_reference_key(value)


def resolve_email_evidence(extraction, scopes=(), *, _include_baseline_references=False):
    if extraction.get("specificationEvidenceVersion") != EVIDENCE_VERSION:
        raise ValueError("Waiting for evidence-aware extraction")
    manifest = extraction.get("sourceManifest")
    if not isinstance(manifest, list) or any(
        not isinstance(source, dict) or source.get("status") not in COMPLETE_SOURCE_STATUSES
        or not isinstance(source.get("text"), str) or not source.get("sourceId") for source in manifest
    ):
        raise ValueError("Incomplete source manifest; retry extraction before publication")
    sources = {source["sourceId"]: source for source in manifest}
    if len(sources) != len(manifest) or not sources or not any(source["status"] == "read" for source in manifest):
        raise ValueError("Source manifest is empty or contains duplicate identities")
    assessed = set(extraction.get("specificationAssessedFields") or [])
    client_request = select_client_request(manifest)
    candidates = defaultdict(list)
    rejected = defaultdict(list)
    excluded = defaultdict(list)
    issues = {}
    for value in extraction.get("specificationEvidence") or []:
        evidence = SpecificationEvidence.model_validate(value).model_dump()
        label = evidence["field"]
        if label not in FIELD_NAMES or label == "Incoming Information":
            continue
        def reject(reason):
            rejected[label].append({**evidence, "reason": reason})

        baseline_reference = (
            _include_baseline_references and label == "Drawing Reference" and evidence["role"] == "baseline"
            and tp_reference_revision(evidence["value"], confirmed_crm=evidence["issuer"] == "TaperedPlus")
        )
        described_deck = label == "Decking" and evidence["role"] == "baseline"
        if evidence["role"] != "requested" and not baseline_reference and not described_deck:
            continue  # Non-request facts are retained in extraction, not published.
        source = sources.get(evidence["source_id"])
        if (source is not None and label in CLIENT_FIELDS
                and not client_evidence_applies(evidence, source, sources, client_request, _contains_excerpt)):
            excluded[label].append({**evidence, "reason": "Evidence does not belong to the selected client request"})
            continue
        if (source is None or source["status"] != "read"
                or not _source_contains_excerpt(source, evidence["snippet"])):
            reject("Supporting source/excerpt could not be verified")
            continue
        if label in {"Bauder Roof Num", "Date Received"} and source["sourceType"] != "email_body":
            reject("Evidence does not meet this field's required email/request scope")
            continue
        unconfirmed_reference = False
        if label == "Drawing Reference" and not tp_reference_revision(evidence["value"]):
            # Unprefixed references require an explicit verified TP issuer claim
            # or, for a literal D.S/bare reference, Monday CRM confirmation.
            tp_issued = evidence["issuer"] == "TaperedPlus" and re.search(
                r"Tapered\s*Plus", evidence["snippet"], re.IGNORECASE)
            if (not tp_reference_revision(evidence["value"], confirmed_crm=True)
                    or (evidence["issuer"] == "TaperedPlus" and not tp_issued)):
                reject("Drawing Reference is not verified as TaperedPlus-issued")
                continue
            # Another issuer cannot confirm the reference, so its quotation is moot.
            unconfirmed_reference = not tp_issued
        if (label == "Drawing Reference" and evidence["issuer"] and not unconfirmed_reference
                and not _contains_excerpt(evidence["issuer"], evidence["snippet"])
                and not (evidence["issuer"] == "TaperedPlus" and (
                    re.match(r"TP\s*\d", evidence["value"], re.IGNORECASE)
                    or re.search(r"Tapered\s*Plus", evidence["snippet"], re.IGNORECASE)))):
            reject("Drawing issuer is absent from the supporting excerpt")
            continue
        if not _value_supported(evidence):
            reject("Raw value is absent from the supporting excerpt")
            continue
        # Carrying/upload dates are provenance, not an undated document's fact date.
        observed_at = (source.get("observedAt") if source["sourceType"] == "email_body"
                       and evidence["request_role"] == "direct_request" else None)
        if label in CLIENT_FIELDS and client_request:
            observed_at = client_request["sentAt"]
        containing = attachment_message(source, sources)
        if containing:
            observed_at = containing["sentAt"]
        if evidence["message_date"]:
            try:
                messages = source_email_messages(source, include_plain=True) if source["sourceType"] == "email_body" else []
                identity_context_date = (
                    label in {"Company", "Contact"} and client_request is not None
                    and _normal(evidence["message_date"]) == _normal(client_request["sentDate"])
                    and evidence["request_role"] in {client_request["requestRole"], "document"}
                    and client_evidence_applies(evidence, source, sources, client_request, _contains_excerpt)
                )
                message_verified = (identity_context_date or not messages or any(
                    _normal(message["sentDate"]) == _normal(evidence["message_date"])
                    and _contains_excerpt(evidence["snippet"], message["text"])
                    and evidence["request_role"] in {message["requestRole"], "document"}
                    for message in messages))
                if (not message_verified
                        or (source.get("contentId") and not _has_parent_request_date(evidence, source, sources))
                        or (not any(_normal(evidence["message_date"]) in _normal(text) for text in literal_source_texts(source))
                     and not _has_parent_request_date(evidence, source, sources))
                        or not re.search(r"\b(?:19|20)\d{2}\b", evidence["message_date"])):
                    raise ValueError("Unverified message date")
                timestamp = _parse_message_date(evidence["message_date"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=ZoneInfo("Europe/London"))
                observed_at = timestamp.astimezone(timezone.utc).isoformat()
            except (ValueError, OverflowError):
                reject("Candidate request timestamp could not be verified")
                continue
        candidates[label].append({**evidence, "observedAt": observed_at,
                                  "sourceObservedAt": source.get("observedAt"),
                                  "filename": source["filename"], "contentSha256": source["contentSha256"],
                                  **({"crmConfirmation": "required"} if unconfirmed_reference else {})})
        if label == "Page Size" and re.fullmatch(r"(?:ISO\s*)?[ABC][A-Z]", evidence["value"].strip(), re.IGNORECASE):
            issues[label] = PAGE_SIZE_UNCERTAIN
        if label == "Page Size" and source.get("pdfPageSizes"):
            size = re.fullmatch(r"(?:ISO\s*)?(A[0-6])", evidence["value"].strip(), re.IGNORECASE)
            if size and size[1].upper() not in source["pdfPageSizes"]:
                issues[label] = PAGE_SIZE_CONFLICT

    for label, failures in rejected.items():
        verified = {_candidate_key(evidence) for evidence in candidates[label]}
        for failure in failures:
            failure["status"] = "repaired" if _candidate_key(failure) in verified else "unverified"
            if failure["status"] == "unverified":
                issues[label] = failure["reason"]

    raw, provenance = {}, {}
    extracted = extraction.get("performanceParameters") or {}
    selected_scopes = {_scope_key(scope) for scope in scopes if scope}
    for label in FIELD_NAMES:
        if label == "Incoming Information":
            raw[label], provenance[label] = "", {"status": "blank", "source": "Business Rule"}
            continue
        options = candidates[label]
        if label not in assessed:
            issues[label] = "Field was not assessed across all sources"
        # Select the target roof/drawing, plus project-wide facts. Without a
        # matching target, distinct scoped values remain a reviewable conflict.
        matched = [e for e in options if e["scope"] is not None and _scope_key(e["scope"]) in selected_scopes]
        if matched:
            general = [e for e in options if e["scope"] is None]
            cutoff = max(e["observedAt"] for e in matched) if all(e["observedAt"] for e in matched) else None
            later_general = [e for e in general if cutoff and e["observedAt"] and e["observedAt"] > cutoff]
            options = later_general or matched
            if any(e["observedAt"] is None and _normal(e["value"]) not in {_normal(m["value"]) for m in matched} for e in general):
                issues[label] = UNDATED_SCOPE_CONFLICT
        by_scope = defaultdict(list)
        for evidence in options:
            by_scope[_scope_key(evidence["scope"])].append(evidence)
        current = []
        for scoped in by_scope.values():
            # Explicit supersession is accepted only within the same scope and
            # with a later known request timestamp. Upload IDs never decide.
            superseded = set()
            for newer in scoped:
                for older in scoped:
                    if (older["value"] in newer["supersedes"] and newer["observedAt"] and older["observedAt"]
                            and newer["observedAt"] > older["observedAt"]):
                        superseded.add(older["value"])
            scoped = [e for e in scoped if e["value"] not in superseded]
            times = [e["observedAt"] for e in scoped if e["observedAt"]]
            if times and all(e["observedAt"] for e in scoped):
                newest = max(times)
                scoped = [e for e in scoped if e["observedAt"] == newest]
            current.extend(scoped)
        current = [e for e in current if not any(
            _normal(e["value"]) != _normal(other["value"]) and _equivalent_description(e, other)
            for other in current)]
        distinct = {_excerpt_normal(_comparable_value(label, e["value"])) for e in current}
        if len(distinct) > 1:
            issues[label] = VALUE_CONFLICT
        selected = current[0] if len(distinct) == 1 else None
        if selected is not None and all(e.get("crmConfirmation") == "required" for e in current):
            issues.setdefault(label, UNCONFIRMED_REFERENCE)
        scalar = extracted.get(label, extracted.get(label.replace("U Value", "U-Value")))
        if selected is None and not missing(scalar):
            issues.setdefault(label, "Populated extraction has no verified applicable evidence")
        # Resolved references use the standard '-' for CAD export and CRM matching;
        # the selected evidence below keeps its literal value and excerpt.
        raw[label] = (None if not selected else normalize_reference_dashes(selected["value"])
                      if label in REFERENCE_FIELDS else selected["value"])
        provenance[label] = {
            "source": "Email/Attachment", "status": "conflict" if label in issues else "provided" if selected else "missing",
            "selected": selected, "candidates": list(candidates[label]),
            **({"rejectedCandidates": rejected[label]} if rejected[label] else {}),
            **({"excludedCandidates": excluded[label]} if excluded[label] else {}),
            "rationale": "Most recent applicable request within the confirmed scope; common values may span roofs",
        }
    return raw, provenance, issues


def resolve_baseline_drawing_identity(extraction):
    """Verify an amendment's existing drawing identity, without promoting its specs.

    Historical manufacturer/architect references are not TP drawing identities.
    Every accepted baseline reference passes the normal citation, value, issuer
    and date checks. Different references remain ambiguous even across dates:
    chronology alone does not establish which existing drawing is being amended.
    """
    _, provenance, issues = resolve_email_evidence(extraction, _include_baseline_references=True)
    reference = provenance["Drawing Reference"]
    candidates = reference["candidates"]
    reason = issues.get("Drawing Reference")
    if len({drawing_reference_key(candidate["value"]) for candidate in candidates}) > 1:
        reason = VALUE_CONFLICT
    return candidates, reason
