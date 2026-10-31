"""Bounded, audited repair of extraction evidence against the immutable inventory.

The model may correct a citation, never make an unsupported claim publishable.
Changes to facts, roof, role or request chronology remain separate claims. An
unsupported TP issuer can be corrected only to literal source branding.
"""

import re

from .performance_specification_evidence import (
    _contains_excerpt, _excerpt_normal, _normal, _value_supported, resolve_email_evidence, resolve_request_notes,
)
from .specification_semantics import EVIDENCE_VERSION, canonical_label


def extraction_payload(result, manifest):
    return {
        "specificationEvidenceVersion": EVIDENCE_VERSION,
        "specificationAssessedFields": result.specification_assessed_fields,
        "specificationEvidence": [e.model_dump() for e in result.specification_evidence],
        "specificationRequestNotes": [n.model_dump() for n in result.specification_request_notes],
        "performanceParameters": {**{canonical_label(k): v for k, v in result.as_canonical_parameters().items()},
                                  "Bauder Roof Num": result.bauder_roof_num},
        "sourceManifest": manifest,
    }


def evidence_diagnostics(result, manifest):
    payload = extraction_payload(result, manifest)
    _, provenance, issues = resolve_email_evidence(payload)
    return provenance, issues


def _same_value(old, new):
    if _excerpt_normal(old.value) == _excerpt_normal(new["value"]):
        return True
    if old.field not in {"Membrane", "VCL", "Tapered Insulation"}:
        return False
    # Only material-list separators may differ. Keep identifiers, ratios, units
    # and signed numeric facts intact; punctuation is significant elsewhere.
    tokens = lambda value: re.findall(r"\w+(?:[./:]\w+)*", value.casefold())
    numbers = lambda value: re.findall(r"(?<!\w)[+-]?\s*\d+(?:[.,]\d+)?", value)
    return tokens(old.value) == tokens(new["value"]) and numbers(old.value) == numbers(new["value"])


def merge_recovery(initial, recovery, manifest):
    before, _ = evidence_diagnostics(initial, manifest)
    after, _ = evidence_diagnostics(recovery, manifest)
    sources = {s["sourceId"]: s for s in manifest}
    retained = []
    for old in initial.specification_evidence:
        failed = any(e["status"] == "unverified" and e["snippet"] == old.snippet
                     and e["source_id"] == old.source_id and e["value"] == old.value
                     for e in before[old.field].get("rejectedCandidates", []))
        replacements = []
        if failed:
            for new in after[old.field].get("candidates", []):
                if any(_normal(getattr(old, k)) != _normal(new[k]) for k in
                       ("scope", "role", "request_role", "message_date")):
                    continue
                if _normal(old.issuer) != _normal(new["issuer"]):
                    if not (old.field == "Drawing Reference" and old.issuer == "TaperedPlus"
                            and new["issuer"] and _contains_excerpt(new["issuer"], new["snippet"])
                            and not re.search(r"Tapered\s*Plus", old.snippet, re.IGNORECASE)):
                        continue
                if not _same_value(old, new):
                    continue
                if old.source_id != new["source_id"]:
                    # A unique passage within the same email can repair a wrong
                    # attachment ordinal. Never cross emails or choose by filename.
                    parent = old.source_id.split(":attachment:", 1)[0]
                    if (":attachment:" not in old.source_id or
                            not new["source_id"].startswith(parent + ":attachment:")):
                        continue
                    matches = [s for s in manifest if s["sourceId"].startswith(parent + ":attachment:")
                               and s["status"] == "read" and _contains_excerpt(new["snippet"], s["text"])]
                    if len(matches) != 1:
                        continue
                replacements.append(new)
        if len(replacements) == 1:
            new = replacements[0]
            initial._evidence_repairs.append({"reason": "Verified citation/formatting repair",
                "before": old.model_dump(), "after": new,
                "contentSha256": sources[new["source_id"]]["contentSha256"]})
        else:
            retained.append(old)
    unique = {e.model_dump_json(): e for e in (*retained, *recovery.specification_evidence)}
    initial.specification_evidence = list(unique.values())
    # A populated scalar may be withdrawn only when a literal, verified
    # non-request candidate identifies that exact fact (e.g. achieved U-value).
    for name in type(initial).model_fields:
        if name.startswith("specification_"):
            continue
        value = getattr(initial, name)
        if value is None or getattr(recovery, name) is not None:
            continue
        label = next((canonical_label(k) for k in recovery.as_canonical_parameters()
                      if k.lower().replace("-", "_").replace(" ", "_") == name), None)
        if not label or any(e.field == label and e.role == "requested" for e in initial.specification_evidence):
            continue
        for evidence in recovery.specification_evidence:
            source = sources.get(evidence.source_id)
            if (evidence.field == label and evidence.role in {"achieved", "baseline", "withdrawn", "lookup"}
                    and _normal(value) == _normal(evidence.value) and source and source["status"] == "read"
                    and _contains_excerpt(evidence.snippet, source["text"]) and _value_supported(evidence.model_dump())):
                setattr(initial, name, None)
                initial._evidence_repairs.append({"reason": "Verified fact is not a current request",
                    "field": label, "before": value, "after": None, "evidence": evidence.model_dump(),
                    "contentSha256": source["contentSha256"]})
                break
    notes = {n.model_dump_json(): n for n in (*initial.specification_request_notes, *recovery.specification_request_notes)}
    initial.specification_request_notes = list(notes.values())
    resolve_request_notes(extraction_payload(initial, manifest))
