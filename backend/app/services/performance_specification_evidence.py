"""Validate source provenance and resolve field candidates without merging roofs."""

from __future__ import annotations

from collections import defaultdict
from datetime import timezone
import re
from zoneinfo import ZoneInfo

from dateutil.parser import parse as parse_datetime

from .specification_semantics import COMPLETE_SOURCE_STATUSES, EVIDENCE_VERSION, SpecificationEvidence, tp_reference_revision
from .performance_specification_contract import FIELD_NAMES, missing


VALUE_CONFLICT = "Conflicting roof/drawing/request values require a confirmed target"
UNDATED_SCOPE_CONFLICT = "Undated project-wide value conflicts with the selected roof"


def _normal(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _excerpt_normal(value):
    # PDF text readers may add Markdown emphasis/code around labels or cells.
    # Ignore only paired presentation markers, preserving numbers, punctuation,
    # table separators and word order. Never join excerpts across an ellipsis.
    text = re.sub(r"\*\*([^*\n]+)\*\*|`([^`\n]+)`", lambda m: m[1] or m[2], str(value or ""))
    return _normal(text)


def _contains_excerpt(excerpt, text):
    excerpt, text = _excerpt_normal(excerpt), _excerpt_normal(text)
    if not excerpt:
        return False
    # A short value such as A1 or 1:10 cannot match the prefix of A10 or 1:100.
    start = r"(?<!\w)" if re.match(r"\w", excerpt[0]) else ""
    end = r"(?!\w)" if re.match(r"\w", excerpt[-1]) else ""
    return re.search(start + re.escape(excerpt) + end, text) is not None


def _candidate_key(evidence):
    # A repaired excerpt may replace a rejected one only for the same claim.
    # Different values, sources, scopes, roles or dates still require review.
    return tuple(evidence.get(key) if key == "source_id" else _normal(evidence.get(key)) for key in (
        "field", "value", "source_id", "scope", "issuer", "role", "request_role", "message_date",
    ))


def _has_parent_request_date(evidence, source, sources):
    """Verify an attachment's inherited date against its own outer email header."""
    source_id = source["sourceId"]
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
    return value.removeprefix("tp") if re.match(r"^(?:tp)?\d+_", value) else value


def resolve_email_evidence(extraction, scopes=()):
    if extraction.get("specificationEvidenceVersion") != EVIDENCE_VERSION:
        raise ValueError("Waiting for evidence-aware extraction (output-v9)")
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
    candidates = defaultdict(list)
    rejected = defaultdict(list)
    issues = {}
    for value in extraction.get("specificationEvidence") or []:
        evidence = SpecificationEvidence.model_validate(value).model_dump()
        label = evidence["field"]
        if label not in FIELD_NAMES or label == "Incoming Information":
            continue
        def reject(reason):
            rejected[label].append({**evidence, "reason": reason})

        source = sources.get(evidence["source_id"])
        if (source is None or source["status"] != "read"
                or not _contains_excerpt(evidence["snippet"], source["text"])):
            reject("Supporting source/excerpt could not be verified")
            continue
        if evidence["role"] != "requested":
            continue  # Achieved figures and unselected lookup options are not requests.
        if label in {"Bauder Roof Num", "Date Received"} and source["sourceType"] != "email_body":
            reject("Evidence does not meet this field's required email/request scope")
            continue
        if label in {"Company", "Contact", "Date Received"} and evidence["request_role"] != "direct_request":
            continue
        if label == "Drawing Reference" and not tp_reference_revision(evidence["value"]):
            # Unprefixed references require an explicit verified TP issuer claim.
            if (evidence["issuer"] != "TaperedPlus" or not re.search(r"Tapered\s*Plus", evidence["snippet"], re.IGNORECASE)
                    or not tp_reference_revision(evidence["value"], confirmed_crm=True)):
                reject("Drawing Reference is not verified as TaperedPlus-issued")
                continue
        if label != "Date Received" and not _contains_excerpt(evidence["value"], evidence["snippet"]):
            reject("Raw value is absent from the supporting excerpt")
            continue
        observed_at = source.get("observedAt")
        if evidence["message_date"]:
            try:
                if ((_normal(evidence["message_date"]) not in _normal(source["text"])
                     and not _has_parent_request_date(evidence, source, sources))
                        or not re.search(r"\b(?:19|20)\d{2}\b", evidence["message_date"])):
                    raise ValueError("Unverified message date")
                timestamp = parse_datetime(evidence["message_date"], dayfirst=True)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=ZoneInfo("Europe/London"))
                observed_at = timestamp.astimezone(timezone.utc).isoformat()
            except (ValueError, OverflowError):
                reject("Candidate request timestamp could not be verified")
                continue
        candidates[label].append({**evidence, "observedAt": observed_at,
                                  "filename": source["filename"], "contentSha256": source["contentSha256"]})

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
        distinct = {_normal(e["value"]) for e in current}
        if len(distinct) > 1:
            issues[label] = VALUE_CONFLICT
        selected = current[0] if len(distinct) == 1 else None
        scalar = extracted.get(label, extracted.get(label.replace("U Value", "U-Value")))
        if selected is None and not missing(scalar):
            issues.setdefault(label, "Populated extraction has no verified applicable evidence")
        raw[label] = selected["value"] if selected else None
        provenance[label] = {
            "source": "Email/Attachment", "status": "conflict" if label in issues else "provided" if selected else "missing",
            "selected": selected, "candidates": list(candidates[label]),
            **({"rejectedCandidates": rejected[label]} if rejected[label] else {}),
            "rationale": "Most recent applicable request within the confirmed scope; common values may span roofs",
        }
    return raw, provenance, issues
