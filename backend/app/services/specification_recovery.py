"""Bounded, audited repair of extraction evidence against the immutable inventory.

The model may correct a citation, never make an unsupported claim publishable.
Changes to facts, roof, role or request chronology remain separate claims. An
unsupported TP issuer can be corrected only to literal source branding.
"""

import re

from .performance_specification_evidence import (
    _contains_excerpt, _excerpt_normal, _normal, _parse_message_date, _value_supported, request_note_diagnostics,
    resolve_email_evidence, resolve_request_notes,
)
from .specification_semantics import EVIDENCE_VERSION, canonical_label, missing, tp_reference_revision


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


def _list_text_positions(text):
    """Normalize only list presentation, retaining offsets into the literal source."""
    omitted = set()
    for match in re.finditer(r"(?m)^[ \t]*[*•][ \t]+|(?<=[a-zA-Z)])\.(?=\s|$)", text):
        omitted.update(range(match.start(), match.end()))
    chars, positions = [], []
    for index, char in enumerate(text):
        if index in omitted:
            continue
        if char.isspace():
            if not chars or chars[-1] == " ":
                continue
            char = " "
        for normalized in char.casefold():
            chars.append(normalized)
            positions.append(index)
    if chars and chars[-1] == " ":
        chars.pop()
        positions.pop()
    return "".join(chars), positions


def repair_note_list_formatting(result, manifest):
    """Recover a unique contiguous list passage without discarding any note words.

    Outlook bullet prefixes and model-added sentence stops can prevent a literal
    match. Restore the full source span, not a shortened model paraphrase. URLs,
    signed numbers, negations, scope and category remain unchanged.
    """
    issues = request_note_diagnostics(extraction_payload(result, manifest))
    sources = {s["sourceId"]: s for s in manifest}
    normalized_sources = {}
    for index, old in enumerate(result.specification_request_notes):
        source = sources.get(old.source_id)
        if (issues.get(index) != "Request note has an unverified source/excerpt"
                or old.category == "source_link" or re.search(r"https?://", old.snippet, re.IGNORECASE)
                or not source or source["status"] != "read"):
            continue
        if old.source_id not in normalized_sources:
            normalized_sources[old.source_id] = _list_text_positions(source["text"])
        normalized, positions = normalized_sources[old.source_id]
        key, _ = _list_text_positions(old.snippet)
        if not key:
            continue
        matches = list(re.finditer(r"(?<!\w)" + re.escape(key) + r"(?!\w)", normalized))
        if len(matches) != 1:
            continue
        match = matches[0]
        snippet = source["text"][positions[match.start()]:positions[match.end() - 1] + 1]
        if len(snippet) > 2000 or not re.search(r"(?m)^[ \t]*[*•][ \t]+", snippet):
            continue
        new = old.model_copy(update={"snippet": snippet})
        payload = {**extraction_payload(result, manifest), "specificationRequestNotes": [new.model_dump()]}
        if request_note_diagnostics(payload):
            continue
        result.specification_request_notes[index] = new
        result._evidence_repairs.append({"reason": "Verified contiguous list-formatting repair",
            "before": old.model_dump(), "after": new.model_dump(),
            "contentSha256": source["contentSha256"]})


def _same_value(old, new):
    if _excerpt_normal(old.value) == _excerpt_normal(new["value"]):
        return True
    if old.field == "Drawing Title":
        # Restore a possessive apostrophe that the model dropped from a name.
        # Keep every letter, number and other punctuation mark; this is not a
        # title alias, identifier rewrite or contraction/negation correction.
        possessive = lambda value: re.sub(r"\b([a-z]{3,})['\u2019]s\b", r"\1s", _excerpt_normal(value))
        return possessive(old.value) == possessive(new["value"])
    if old.field not in {"Membrane", "VCL", "Tapered Insulation"}:
        return False
    # Only material-list separators may differ. Keep identifiers, ratios, units
    # and signed numeric facts intact; punctuation is significant elsewhere.
    tokens = lambda value: re.findall(r"\w+(?:[./:]\w+)*", value.casefold())
    numbers = lambda value: re.findall(r"(?<!\w)[+-]?\s*\d+(?:[.,]\d+)?", value)
    return tokens(old.value) == tokens(new["value"]) and numbers(old.value) == numbers(new["value"])


def _repair_source_allowed(old_id, new_id, snippet, manifest):
    if old_id == new_id:
        return True
    # A unique passage within the same email can repair a wrong attachment
    # ordinal. Never cross emails or choose by filename.
    parent = old_id.split(":attachment:", 1)[0]
    if ":attachment:" not in old_id or not new_id.startswith(parent + ":attachment:"):
        return False
    matches = [s for s in manifest if s["sourceId"].startswith(parent + ":attachment:")
               and s["status"] == "read" and _contains_excerpt(snippet, s["text"])]
    return len(matches) == 1 and matches[0]["sourceId"] == new_id


def _note_text_key(note):
    if note.category == "source_link":
        # URL paths and queries can be case-sensitive.
        return re.sub(r"\s+", " ", note.snippet).strip()
    text = _excerpt_normal(note.snippet)
    # Permit a model-added label colon only. Keep all words (including
    # negations), numbers, signs, ratios and sentence boundaries.
    return re.sub(r"(?<=[a-z]):(?=\s)", "", text)


def _note_scope_matches(old, new, sources):
    if _normal(old.scope) == _normal(new.scope):
        return True
    source = sources.get(old.source_id)
    # Recovery may remove an invented optional scope, never replace a verified
    # roof or move its constraint to another source. The full excerpt survives.
    return (new.scope is None and old.scope and old.source_id == new.source_id
            and source and source["status"] == "read"
            and _contains_excerpt(old.snippet, source["text"])
            and not _contains_excerpt(old.scope, source["text"]))


def _date_repair_allowed(old, new, sources):
    if _normal(old.message_date) == _normal(new["message_date"]):
        return True
    source = sources.get(old.source_id)
    if (not old.message_date or not new["message_date"] or old.source_id != new["source_id"]
            or not source or source["sourceType"] != "email_body"
            or _contains_excerpt(old.message_date, source["text"])):
        return False
    try:
        if any(not re.search(r"\b(?:19|20)\d{2}\b", value) for value in (old.message_date, new["message_date"])):
            return False
        if (_parse_message_date(old.message_date).replace(tzinfo=None)
                != _parse_message_date(new["message_date"]).replace(tzinfo=None)):
            return False
    except (ValueError, OverflowError):
        return False
    text = source["text"].replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"(?m)(?=^From:[ \t])", text)
    blocks = [block for block in blocks if block.startswith("From:")]
    matches = []
    for index, block in enumerate(blocks):
        headers = []
        for line in block.splitlines():
            if not line.strip():
                continue
            if re.match(r"^(?:From|To|Cc|Bcc|Subject|Date|Sent):", line, re.IGNORECASE):
                headers.append(line)
            elif line[0].isspace():
                continue  # Folded header continuation.
            else:
                break
        dates = [match[1] for line in headers
                 if (match := re.match(r"^(?:Date|Sent):[ \t]*(.+)$", line, re.IGNORECASE))]
        if (len(dates) == 1 and _normal(dates[0]) == _normal(new["message_date"])
                and _contains_excerpt(new["snippet"], block)
                and ((old.request_role == "forwarded_request" and index > 0)
                     or (old.request_role == "direct_request" and index == 0))):
            matches.append(index)
    return len(matches) == 1


def _issuer_repair_allowed(old, new):
    if _normal(old.issuer) == _normal(new["issuer"]):
        return True
    # A self-identifying TP/AX reference may retain its exact value while the
    # recovery removes an unquoted issuer or quotes the literal source branding.
    # Bare/D.S references still need their verified TP issuer to validate.
    return (old.field == "Drawing Reference" and old.issuer
            and tp_reference_revision(old.value) is not None
            and not _contains_excerpt(old.issuer, old.snippet)
            and not (old.issuer == "TaperedPlus" and re.search(r"Tapered\s*Plus", old.snippet, re.IGNORECASE))
            and (new["issuer"] is None or _contains_excerpt(new["issuer"], new["snippet"])))


def _merge_request_notes(initial, recovery, manifest):
    repair_note_list_formatting(initial, manifest)
    repair_note_list_formatting(recovery, manifest)
    initial._evidence_repairs.extend(recovery._evidence_repairs)
    before = request_note_diagnostics(extraction_payload(initial, manifest))
    after = request_note_diagnostics(extraction_payload(recovery, manifest))
    sources = {s["sourceId"]: s for s in manifest}
    retained = []
    for index, old in enumerate(initial.specification_request_notes):
        replacements = {new.model_dump_json(): new
            for new_index, new in enumerate(recovery.specification_request_notes)
            if index in before and new_index not in after
            and old.category == new.category and _note_scope_matches(old, new, sources)
            and _note_text_key(old) == _note_text_key(new)
            and _repair_source_allowed(old.source_id, new.source_id, new.snippet, manifest)}
        if len(replacements) == 1:
            new = next(iter(replacements.values()))
            initial._evidence_repairs.append({
                "reason": "Verified request-note citation/formatting repair",
                "before": old.model_dump(), "after": new.model_dump(),
                "contentSha256": sources[new.source_id]["contentSha256"],
            })
        else:
            retained.append(old)
    notes = {n.model_dump_json(): n for n in (*retained, *recovery.specification_request_notes)}
    initial.specification_request_notes = list(notes.values())
    resolve_request_notes(extraction_payload(initial, manifest))


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
                       ("scope", "role", "request_role")):
                    continue
                if not _date_repair_allowed(old, new, sources) or not _issuer_repair_allowed(old, new):
                    continue
                if not _same_value(old, new):
                    continue
                if not _repair_source_allowed(old.source_id, new["source_id"], new["snippet"], manifest):
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
        if missing(value) or not missing(getattr(recovery, name)):
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
    _merge_request_notes(initial, recovery, manifest)
