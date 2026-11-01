"""Bounded, audited repair of extraction evidence against the immutable inventory.

The model may correct a citation, never make an unsupported claim publishable.
Changes to facts, roof, role or request chronology remain separate claims. An
unsupported TP issuer can be corrected only to literal source branding.
"""

import re

from .performance_specification_evidence import (
    _contains_excerpt, _excerpt_normal, _normal, _parse_message_date, _value_supported, request_note_diagnostics,
    resolve_email_evidence, resolve_request_notes, _source_contains_excerpt,
)
from .specification_semantics import (
    EVIDENCE_VERSION, REFERENCE_FIELDS, U_VALUE_FIELDS, canonical_label, missing, normalize_reference_dashes,
    normalize_unit_spacing, scale_ratio, tp_reference_revision,
)
from .specification_image_text import literal_image_regions
from .specification_request_context import (
    attachment_message, attachment_messages, parent_email, request_context_summary, select_client_request, source_email_messages,
)


def extraction_payload(result, manifest):
    return {
        "specificationEvidenceVersion": EVIDENCE_VERSION,
        "specificationAssessedFields": result.specification_assessed_fields,
        "specificationEvidence": [e.model_dump() for e in result.specification_evidence],
        "specificationRequestNotes": [n.model_dump() for n in result.specification_request_notes],
        "performanceParameters": {**{canonical_label(k): v for k, v in result.as_canonical_parameters().items()},
                                  "Bauder Roof Num": result.bauder_roof_num},
        "sourceManifest": manifest,
        "requestContext": request_context_summary(select_client_request(manifest)),
    }


def evidence_diagnostics(result, manifest):
    payload = extraction_payload(result, manifest)
    _, provenance, issues = resolve_email_evidence(payload)
    return provenance, issues


def bound_verified_request_notes(data, manifest):
    """Split an oversized literal citation without truncating a constraint."""
    sources = {s["sourceId"]: s for s in manifest or []}
    if not isinstance(data, dict) or not isinstance(data.get("specification_request_notes", []), list):
        return data, []  # The original model schema reports the type error.
    notes, repairs = [], []
    for note in data.get("specification_request_notes", []):
        text = note.get("snippet") if isinstance(note, dict) else None
        source = sources.get(note.get("source_id")) if isinstance(note, dict) else None
        if (not isinstance(text, str) or len(text) <= 2000 or not source or source["status"] != "read"
                or not _source_contains_excerpt(source, text)):
            notes.append(note)
            continue
        pieces = []
        remaining = text
        while len(remaining) > 2000:
            # Keep words, signed numbers and URLs intact. An unbroken long token
            # stays invalid; truncation would turn it into a different claim.
            stops = list(re.finditer(r"\s+", remaining[:2001]))
            if not stops:
                pieces = []
                break
            stop = stops[-1].start()
            if stop == 0:
                pieces = []
                break
            pieces.append({**note, "snippet": remaining[:stop]})
            remaining = remaining[stops[-1].end():]
        if not pieces:
            notes.append(note)
            continue
        if remaining:
            pieces.append({**note, "snippet": remaining})
        notes.extend(pieces)
        repairs.append({"reason": "Split a verified contiguous note into bounded citations",
                        "before": note, "after": pieces, "contentSha256": source["contentSha256"]})
    return {**data, "specification_request_notes": notes}, repairs


def repair_image_company_citations(result, manifest):
    """Restore one complete literal image region; never change the claimed name."""
    sources = {source["sourceId"]: source for source in manifest}
    for index, old in enumerate(result.specification_evidence):
        source = sources.get(old.source_id)
        if (old.field == "Company" and source and source["status"] == "read"
                and source["sourceType"] == "email_body" and not _value_supported(old.model_dump())
                and _source_contains_excerpt(source, old.snippet)):
            # A URL cannot establish a company name. The exact claimed branding
            # may instead be in one CID-linked logo in that same signature.
            messages = [m for m in source_email_messages(source)
                        if _contains_excerpt(old.snippet, m["text"])
                        and old.request_role in {m["requestRole"], "document"}
                        and (not old.message_date or _normal(old.message_date) == _normal(m["sentDate"]))]
            matches = []
            if len(messages) == 1 and messages[0]["sender"] and messages[0]["sentAt"]:
                for image in manifest:
                    if (image["status"] != "read" or not image.get("imageTextRegions")
                            or parent_email(image, sources) is not source):
                        continue
                    associated = attachment_messages(image, sources)
                    if (not any(all(m[k] == messages[0][k] for k in ("sender", "sentAt", "messageIndex")) for m in associated)
                            or any(m["sender"] != messages[0]["sender"] for m in associated)):
                        continue
                    for region in literal_image_regions(image["text"]):
                        if _contains_excerpt(old.value, region["text"]):
                            matches.append((image, region))
            if len(matches) == 1:
                image, region = matches[0]
                new = old.model_copy(update={"source_id": image["sourceId"], "snippet": region["text"],
                                             "location": "TEXT REGION " + region["id"]})
                result.specification_evidence[index] = new
                result._evidence_repairs.append({"reason": "Verified same-message inline company citation repair",
                    "before": old.model_dump(), "after": new.model_dump(),
                    "contentSha256": image["contentSha256"], "parentEmailContentSha256": source["contentSha256"]})
                continue
        if (old.field != "Company" or not source or source["status"] != "read"
                or not source.get("imageTextRegions")):
            continue
        if _source_contains_excerpt(source, old.snippet) and _value_supported(old.model_dump()):
            continue
        # Reparse immutable text; metadata alone cannot supply a fabricated region.
        matches = [region for region in literal_image_regions(source["text"])
                   if _contains_excerpt(old.value, region["text"])]
        if len(matches) != 1:
            continue
        new = old.model_copy(update={"snippet": matches[0]["text"]})
        result.specification_evidence[index] = new
        result._evidence_repairs.append({
            "reason": "Verified literal image-region citation repair", "regionId": matches[0]["id"],
            "before": old.model_dump(), "after": new.model_dump(),
            "contentSha256": source["contentSha256"],
        })


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
    if old.field in REFERENCE_FIELDS:
        # A typographic dash in a drawing reference is the standard '-'.
        return (_excerpt_normal(normalize_reference_dashes(old.value))
                == _excerpt_normal(normalize_reference_dashes(new["value"])))
    if old.field in U_VALUE_FIELDS:
        # Number-unit spacing only; the number and unit text must be identical.
        return (_excerpt_normal(normalize_unit_spacing(old.value))
                == _excerpt_normal(normalize_unit_spacing(new["value"])))
    if old.field == "Drawing Title":
        # Restore a possessive apostrophe that the model dropped from a name.
        # Keep every letter, number and other punctuation mark; this is not a
        # title alias, identifier rewrite or contraction/negation correction.
        possessive = lambda value: re.sub(r"\b([a-z]{3,})['\u2019]s\b", r"\1s", _excerpt_normal(value))
        return possessive(old.value) == possessive(new["value"])
    if old.field == "Scale":
        # Ratio notation only (1/50 = 1:50); the ordered terms must be identical.
        return scale_ratio(old.value) is not None and scale_ratio(old.value) == scale_ratio(new["value"])
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
            or not source
            or _source_contains_excerpt(source, old.message_date)):
        return False
    try:
        if any(not re.search(r"\b(?:19|20)\d{2}\b", value) for value in (old.message_date, new["message_date"])):
            return False
        if (_parse_message_date(old.message_date).replace(tzinfo=None)
                != _parse_message_date(new["message_date"]).replace(tzinfo=None)):
            return False
    except (ValueError, OverflowError):
        return False
    if source["sourceType"] == "attachment":
        message = attachment_message(source, sources)
        if not source.get("contentId") and old.request_role != "forwarded_request":
            parent = parent_email(source, sources)
            if parent and source.get("observedAt") == parent.get("observedAt"):
                message = next((m for m in source_email_messages(parent) if m["messageIndex"] == 0), None)
        return (message is not None and _normal(message["sentDate"]) == _normal(new["message_date"])
                and _source_contains_excerpt(source, new["snippet"])
                and old.request_role in {message["requestRole"], "document"})
    if source["sourceType"] != "email_body":
        return False
    matches = {(m["sender"], m["sentAt"], m["messageIndex"]) for m in source_email_messages(source)
               if _normal(m["sentDate"]) == _normal(new["message_date"])
               and _contains_excerpt(new["snippet"], m["text"])
               and old.request_role == m["requestRole"]}
    return len(matches) == 1


def repair_message_dates(result, manifest):
    """Restore a uniquely verified literal header, never guess a fact's date."""
    sources = {s["sourceId"]: s for s in manifest}
    for index, old in enumerate(result.specification_evidence):
        source = sources.get(old.source_id)
        if not old.message_date or not source or source["status"] != "read":
            continue
        parent = source if source["sourceType"] == "email_body" else parent_email(source, sources)
        if not parent:
            continue
        if source["sourceType"] == "email_body":
            # A model can attach a quoted passage to the outer reply's literal
            # date. Correct that citation only when it belongs to one distinct,
            # addressed message and the old header demonstrably lacks the quote.
            # Invented dates and passages repeated across replies remain invalid.
            messages = source_email_messages(source, include_plain=True)
            old_messages = [m for m in messages if _normal(m["sentDate"]) == _normal(old.message_date)]
            matching = {(m["sender"], m["sentAt"], m["messageIndex"], m["requestRole"]): m
                        for m in messages if _contains_excerpt(old.snippet, m["text"])}
            if (old.field not in {"Company", "Contact", "Date Received"}
                    and old_messages and len(matching) == 1
                    and not any(_contains_excerpt(old.snippet, m["text"]) for m in old_messages)):
                message = next(iter(matching.values()))
                if message["sender"] and message["sentAt"]:
                    new = old.model_copy(update={"message_date": message["sentDate"],
                                                 "request_role": message["requestRole"]})
                    result.specification_evidence[index] = new
                    result._evidence_repairs.append({"reason": "Verified unique containing-message citation repair",
                        "before": old.model_dump(), "after": new.model_dump(),
                        "contentSha256": source["contentSha256"],
                        **({"emailHtmlSha256": source["emailHtmlSha256"]} if source.get("emailHtmlSha256") else {})})
                    continue
        replacements = {}
        for message in source_email_messages(parent):
            if _normal(message["sentDate"]) == _normal(old.message_date):
                continue
            proposed = old.model_copy(update={"message_date": message["sentDate"]})
            if _date_repair_allowed(old, proposed.model_dump(), sources):
                replacements[message["sentDate"]] = proposed
        if len(replacements) == 1:
            new = next(iter(replacements.values()))
            result.specification_evidence[index] = new
            result._evidence_repairs.append({"reason": "Verified literal message-date formatting repair",
                "before": old.model_dump(), "after": new.model_dump(),
                "contentSha256": source["contentSha256"], "parentEmailContentSha256": parent["contentSha256"]})


def _issuer_repair_allowed(old, new):
    if _normal(old.issuer) == _normal(new["issuer"]):
        return True
    # A self-identifying TP/AX reference may retain its exact value while the
    # recovery removes an unquoted issuer or quotes the literal source branding.
    # Bare/D.S references need a verified TP issuer or Monday CRM confirmation.
    return (old.field == "Drawing Reference" and old.issuer
            and tp_reference_revision(old.value) is not None
            and not _contains_excerpt(old.issuer, old.snippet)
            and not (old.issuer == "TaperedPlus" and re.search(r"Tapered\s*Plus", old.snippet, re.IGNORECASE))
            and (new["issuer"] is None or _contains_excerpt(new["issuer"], new["snippet"])))


def _merge_request_notes(initial, recovery, manifest):
    repair_note_list_formatting(initial, manifest)
    repair_note_list_formatting(recovery, manifest)
    initial._evidence_repairs.extend(r for r in recovery._evidence_repairs if r not in initial._evidence_repairs)
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
    repair_image_company_citations(initial, manifest)
    repair_image_company_citations(recovery, manifest)
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
            parent = parent_email(sources[new["source_id"]], sources)
            initial._evidence_repairs.append({"reason": "Verified citation/formatting repair",
                "before": old.model_dump(), "after": new,
                "contentSha256": sources[new["source_id"]]["contentSha256"],
                **({"parentEmailContentSha256": parent["contentSha256"]} if parent else {})})
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
            if label == "Decking" and evidence.role == "baseline":
                continue  # Described existing decking remains an applicable fact.
            if (evidence.field == label and evidence.role in {"achieved", "baseline", "withdrawn", "lookup"}
                    and _normal(value) == _normal(evidence.value) and source and source["status"] == "read"
                    and _contains_excerpt(evidence.snippet, source["text"]) and _value_supported(evidence.model_dump())):
                setattr(initial, name, None)
                initial._evidence_repairs.append({"reason": "Verified fact is not a current request",
                    "field": label, "before": value, "after": None, "evidence": evidence.model_dump(),
                    "contentSha256": source["contentSha256"]})
                break
    _merge_request_notes(initial, recovery, manifest)
