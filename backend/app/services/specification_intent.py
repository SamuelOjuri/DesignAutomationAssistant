"""Cited request-intent recommendations, separate from human classification.

Only the selected external client request can establish intent. Older forwarded
messages can supply search references, but cannot turn a new request into an
amendment. Directory matches remain supporting evidence, never intent scores.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .specification_request_context import parent_email, select_client_request, source_email_messages
from .specification_review_contract import (
    REQUEST_INTENT_SCHEMA_VERSION, DetectedReference, EmailCitation, ProjectMatch,
    RequestIntentRecommendation,
)


REQUEST_INTENT_PROMPT = (
    "In specification_request_intent recommend new_enquiry, amendment or uncertain. "
    "Use the selected external client's current request and its applicable attachments. "
    "A request to adjust/change an existing scheme can be an amendment without using "
    "the word amendment. A familiar site, customer or drawing reference alone does "
    "not establish an amendment: genuinely new work remains new_enquiry. Quote "
    "verbatim supporting excerpts with exact source IDs and locations. Do not use "
    "an unrelated historical quotation to classify the current request. Where intent "
    "conflicts or sources are incomplete, recommend uncertain and explain why. "
    "List literal detected project and full drawing references separately; preserve "
    "their actual two-letter prefix and leading zeros. Do not invent Monday IDs, "
    "project matches, future revisions or a classification confidence percentage. "
    "This sidecar is an AI recommendation, not the human-owned Monday classification."
)


def _regions(manifest, context):
    """Text regions have verified message boundaries, independently of model claims."""
    sources = {source.get("sourceId"): source for source in manifest}
    regions = []
    if context:
        regions.append((context["sourceId"], "selected client request subject/body",
                        context["text"], "current_request"))
        source = sources[context["sourceId"]]
        for message in source_email_messages(source):
            if (message["text"] != context["text"] and message.get("sentAt")
                    and message["sentAt"] < context["sentAt"]):
                regions.append((context["sourceId"], "earlier forwarded context",
                                message["text"], "forwarded_context"))
    else:
        # Incomplete identity/date headers prevent confident classification, not
        # read-only discovery of literal references for a reviewer.
        for source in manifest:
            if source.get("status") == "read" and source.get("sourceType") == "email_body":
                regions.append((source["sourceId"], "unselected email context",
                                source.get("text", ""), "forwarded_context"))
    for source in manifest:
        if source.get("status") != "read" or source.get("sourceType") == "email_body":
            continue
        parent = parent_email(source, sources)
        # Standalone supplementary documents belong to this input inventory;
        # attachments of an unrelated email do not establish this request.
        if parent and (not context or parent["sourceId"] != context["sourceId"]):
            continue
        if parent and context and context["messageIndex"] != 0:
            # A forwarded message's attachment ownership is otherwise unknown.
            # CID-backed attribution can establish it without a model assertion.
            from .specification_request_context import attachment_message
            message = attachment_message(source, sources)
            if not message or message.get("sentAt") != context["sentAt"] or message.get("sender") != context["sender"]:
                continue
        regions.append((source["sourceId"], "attachment/document text",
                        source.get("text", ""), "document"))
    return regions


def _literal_excerpt(snippet, text):
    # Only line-ending normalization is safe for verbatim citations.
    return snippet.replace("\r\n", "\n").replace("\r", "\n") in text.replace("\r\n", "\n").replace("\r", "\n")


def _citations(citations, regions, *, current_only=False):
    valid = []
    for citation in citations:
        if any(citation.source_id == identity and _literal_excerpt(citation.excerpt, text)
               and (not current_only or role in {"current_request", "document"})
               for identity, _, text, role in regions):
            valid.append(citation)
    return valid


def _detected_references(regions):
    from .specification_references import DrawingReference, find_references

    found = {}
    for identity, location, text, role in regions:
        for match in find_references(text):
            reference = match.reference
            # Keep the smallest literal source line as a reviewable citation.
            start = text.rfind("\n", 0, match.start) + 1
            end = text.find("\n", match.end)
            end = len(text) if end == -1 else end
            start = max(start, match.start - 1500)
            line = text[start:min(end, start + 4000)].strip()
            citation = EmailCitation(source_id=identity, excerpt=line, location=location)
            kind = "drawing" if isinstance(reference, DrawingReference) else "project"
            detected = DetectedReference(kind=kind, literal=reference.literal, evidence=citation, role=role)
            found.setdefault((identity, role, reference.literal), detected)
    return list(found.values())[:256]


def _fallback(context):
    """Conservative compatibility path for producers without the typed sidecar."""
    if not context:
        return "uncertain", [], "No verified external client request is available."
    text = context["text"]
    signals = {"new_enquiry": [], "amendment": []}
    for line in text.splitlines():
        if re.match(r"\s*>|(?:From|To|Cc|Bcc|Date|Sent):", line, re.I):
            continue
        if re.search(r"\b(?:not|no|without|never)\b.{0,25}\b(?:amend|revis|chang|adjust|new enquir)", line, re.I):
            continue
        if re.search(r"\bnew\s+(?:enquir[yi]|inquir[yi]|project|request|work)\b", line, re.I):
            signals["new_enquiry"].append(line.strip())
        if (re.search(r"\b(?:please|could you|can you|would you)\b.{0,60}\b(?:amend|revise|adjust|update|rework|change|alter)\b", line, re.I)
                or re.search(r"\b(?:amend|revise|adjust|update|rework|change|alter)\b.{0,55}\b(?:existing|previous|current|original|scheme|layout|drawing|design)\b", line, re.I)
                or re.search(r"\b(?:amendment|revision)\s+(?:to|of|for|request)\b", line, re.I)):
            signals["amendment"].append(line.strip())
    kinds = [kind for kind, lines in signals.items() if lines]
    citations = [EmailCitation(source_id=context["sourceId"], excerpt=line[:4000],
                              location="selected client request subject/body")
                 for kind in kinds for line in signals[kind][:8]]
    if len(kinds) != 1:
        return "uncertain", citations, ("The current request contains conflicting new-work and change instructions."
                                  if kinds else "The current request does not clearly distinguish new work from a change to an existing scheme.")
    kind = kinds[0]
    explanation = ("The current client request explicitly asks for new work." if kind == "new_enquiry"
                   else "The current client request asks to change an existing scheme.")
    return kind, citations, explanation


def build_request_intent(manifest, proposal=None, *, matched_projects: Sequence[ProjectMatch | dict] = ()):
    """Validate model citations and derive literal references from readable sources.

    A bad sidecar citation downgrades the recommendation; it never blocks the
    independent scalar/evidence extraction or borrows a historical quotation.
    """
    issues = []
    try:
        context = select_client_request(manifest)
    except ValueError as exc:
        context = None
        issues.append(str(exc))
    regions = _regions(manifest, context)
    refs = _detected_references(regions)
    if proposal is not None:
        proposal = RequestIntentRecommendation.model_validate(proposal)
        evidence = _citations(proposal.evidence, regions)
        current = _citations(proposal.evidence, regions, current_only=True)
        issues.extend(proposal.ambiguity_reasons)
        if len(evidence) != len(proposal.evidence):
            issues.append("Some intent citations are absent from the selected request or its applicable attachments.")
        if proposal.recommended_type != "uncertain" and (not context or not current):
            issues.append("Intent requires supporting evidence from the selected external client request.")
        compatible_kind, _, compatible_explanation = _fallback(context)
        if ((compatible_kind != "uncertain" and proposal.recommended_type not in {compatible_kind, "uncertain"})
                or compatible_explanation.startswith("The current request contains conflicting")):
            issues.append("The current request contains conflicting new-work and change instructions.")
        # Retain a model's explicit roof scope only when its complete citation
        # and that scope are literal in a verified source region.
        for detected in proposal.detected_references:
            if not detected.evidence.scope or detected.evidence.scope not in detected.evidence.excerpt:
                continue
            if _citations([detected.evidence], regions) and detected.literal in detected.evidence.excerpt:
                for index, reference in enumerate(refs):
                    if (reference.literal == detected.literal
                            and reference.evidence.source_id == detected.evidence.source_id):
                        refs[index] = reference.model_copy(update={"evidence": detected.evidence})
        kind = "uncertain" if issues else proposal.recommended_type
        explanation = proposal.explanation if not issues else "; ".join(dict.fromkeys(issues))
    else:
        kind, evidence, explanation = _fallback(context)
        if kind == "uncertain":
            issues.append(explanation)
    result = RequestIntentRecommendation(
        schema_version=REQUEST_INTENT_SCHEMA_VERSION, recommended_type=kind,
        explanation=explanation, evidence=evidence, detected_references=refs,
        ambiguity_reasons=list(dict.fromkeys(issues)),
    )
    return enrich_request_intent(result, matched_projects)


def enrich_request_intent(intent, matched_projects: Sequence[ProjectMatch | dict], *, lookup_issues=()):
    """Attach read-only matches without treating similarity as intent probability."""
    intent = RequestIntentRecommendation.model_validate(intent)
    matches = [ProjectMatch.model_validate(match) for match in matched_projects]
    unique = {(match.record.board_id, match.record.item_id): match for match in matches}
    issues = list(intent.ambiguity_reasons) + list(lookup_issues)
    if len(unique) > 1:
        issues.append("Several projects remain plausible; the reviewer must select the relevant project.")
    return intent.model_copy(update={"matched_projects": list(unique.values()),
                                     "ambiguity_reasons": list(dict.fromkeys(issues))})


def reason_for_change(intent):
    kind = RequestIntentRecommendation.model_validate(intent).recommended_type
    return {"new_enquiry": "New Enquiry (AI recommendation)",
            "amendment": "Amendment (AI recommendation)",
            "uncertain": "Reviewer decision required"}[kind]
