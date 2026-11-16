from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata
from typing import Any, Protocol, Sequence

from fastapi import HTTPException

from ...monday_client import (
    MondayProjectBoardItem,
    MondayProjectSearchBudget,
    MondayProjectSearchResult,
)


# A display limit, applied only after all admitted candidates have been ranked.
PROJECT_MATCH_LIMIT = 500
MAX_PROJECT_SEARCH_WORDS = 12


class ProjectMatchingGateway(Protocol):
    def fetch_project_items_matching_references(
        self, references: Sequence[str],
    ) -> tuple[MondayProjectBoardItem, ...]: ...

    def fetch_project_title_candidates(
        self, *, exact_title: str | None = None, words: Sequence[str] = (),
        match_all: bool = False, budget: MondayProjectSearchBudget,
    ) -> MondayProjectSearchResult: ...


ENGLISH_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "by", "for",
        "from", "has", "he", "in", "is", "it", "its", "of", "on", "that",
        "the", "to", "was", "were", "will", "with", "this", "but", "they",
        "have", "had", "what", "said", "each", "which", "she", "do", "how",
        "their", "if", "up", "out", "many", "then", "them", "these", "so",
        "some", "her", "would", "make", "like", "into", "him", "time", "two",
        "more", "go", "no", "way", "could", "my", "than", "call", "who",
        "sit", "now", "find", "down", "day", "did", "get", "come", "made",
        "may", "part",
    }
)

UK_POSTCODE_TOKEN_PATTERNS = (
    r"[A-Z][0-9]{1,2}",
    r"[A-Z][A-HJKPSTUW][0-9]{1,2}",
    r"[A-Z][0-9][A-Z]",
    r"[A-Z][A-HJKPSTUW][0-9][A-Z]",
    r"[0-9][A-Z]{2}",
    r"0[A-Z]{2}",
)

# These words still contribute to title comparison, but cannot establish an
# identity by themselves in a relaxed search. This is deliberately separate
# from stop-word removal: exact titles such as "High Street" remain searchable.
GENERIC_PROJECT_WORDS = frozenset({
    "street", "st", "road", "rd", "avenue", "ave", "lane", "drive", "dr",
    "close", "court", "place", "way", "crescent", "terrace", "building",
    "buildings", "house", "houses", "roof", "roofs", "block", "blocks",
    "phase", "plot", "plots", "site", "project", "development", "industrial",
    "estate", "school", "academy", "college", "university", "hospital",
    "centre", "center", "extension", "refurbishment", "works", "unit", "units",
    "apartment", "apartments",
})
ADDRESS_SUFFIX_WORDS = frozenset({
    "street", "st", "road", "rd", "avenue", "ave", "lane", "drive", "dr",
    "close", "court", "place", "way", "crescent", "terrace",
})


def normalize_project_title(text: str) -> str:
    """Compare whole titles without losing house numbers or expanding St/Saint."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"[^\W_]+", text))


def filter_meaningful_words(text: str) -> list[str]:
    normalized = re.sub(r"\b(St|Dr|Rd)\.\s*", r"\1 ", text)
    tokens = re.findall(r"\b\w+(?:'\w+)*\b", normalized)
    meaningful: list[str] = []
    for word in tokens:
        upper = word.upper()
        lower = word.lower()
        if re.fullmatch(r"\d+[A-Z]?", upper):
            continue
        if upper == "GIR":
            continue
        if any(re.fullmatch(pattern, upper) for pattern in UK_POSTCODE_TOKEN_PATTERNS):
            continue
        if re.fullmatch(r"[A-Z]+\d+", upper):
            continue
        if lower in ENGLISH_STOP_WORDS:
            continue
        if len(word) <= 2 and upper not in {"OF", "ST", "DR", "RD"}:
            continue
        meaningful.append(word)
    return meaningful


def _identifying_words(normalized_title: str) -> tuple[str, ...]:
    def meaningful(text):
        return tuple(dict.fromkeys(word for word in filter_meaningful_words(text)
                                   if word not in GENERIC_PROJECT_WORDS))

    tokens = normalized_title.split()
    for index, word in enumerate(tokens):
        if word in ADDRESS_SUFFIX_WORDS:
            before_address_suffix = meaningful(" ".join(tokens[:index]))
            if before_address_suffix:
                # In "Pomeroy Street Cardiff", Cardiff supplies context; it
                # cannot independently identify a different Cardiff project.
                return before_address_suffix
            break
    return meaningful(normalized_title)


def _identifying_word_agrees(requested: str, candidate: str) -> bool:
    if requested == candidate:
        return True
    # Small spelling differences are suggestions only. A common prefix alone
    # must not admit names such as Pomeroysmith or AshfordHeath.
    return (min(len(requested), len(candidate)) >= 5
            and abs(len(requested) - len(candidate)) <= 1
            and requested[:3] == candidate[:3]
            and SequenceMatcher(None, requested, candidate).ratio() >= 0.88)


def _identifying_coverage(identifying: Sequence[str], tokens: set[str]) -> int:
    return sum(any(_identifying_word_agrees(word, token) for token in tokens) for word in identifying)


def _similarity(project_name: str, item: MondayProjectBoardItem) -> float:
    name_ratio = SequenceMatcher(
        None,
        project_name.lower(),
        item.project_reference.lower(),
    ).ratio()
    title_ratio = (
        SequenceMatcher(
            None,
            project_name.lower(),
            item.project_title.lower(),
        ).ratio()
        if item.project_title
        else 0.0
    )
    return max(name_ratio, title_ratio)


def _legacy_match(
    item: MondayProjectBoardItem,
    project_name: str,
) -> dict[str, Any]:
    return {
        "id": item.item_id,
        "name": item.project_reference,
        "title": item.project_title,
        "similarity": _similarity(project_name, item),
        "state": item.state,
        "created_date": item.created_date,
        **({"reference_confirmations": list(item.reference_confirmations)} if item.reference_confirmations else {}),
    }


def _ordered_matches(
    items: Sequence[MondayProjectBoardItem],
    project_name: str,
    *,
    similarity_threshold: float | None = None,
) -> list[dict[str, Any]]:
    matches = [
        _legacy_match(item, project_name)
        for item in items
        if item.state == "active"
    ]
    if similarity_threshold is not None:
        matches = [
            match
            for match in matches
            if match["similarity"] >= similarity_threshold
        ]
    return sorted(matches, key=lambda match: (
        -match["similarity"], match["title"].casefold(), str(match["name"]), str(match["id"]),
    ))


def _result_with_matches(matches: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": False,
        "type": "new",
        "matches": matches,
        "best_match": None,
        "similarity_score": 0.0,
        "error": "",
    }
    if matches:
        result.update(
            {
                "exists": True,
                "type": "existing",
                "best_match": matches[0],
                "similarity_score": matches[0]["similarity"],
            }
        )
    return result


def _title_search_result(
    matches: list[dict[str, Any]], *, stage: str, budget: MondayProjectSearchBudget,
    complete: bool = True, detail: str = "",
) -> dict[str, Any]:
    result = _result_with_matches(matches)
    result["titleLookup"] = {
        "status": "available" if complete else "incomplete", "stage": stage,
        "pagesRead": budget.pages_read, "maxPages": budget.max_pages,
        "maxSeconds": budget.max_seconds,
        **({"detail": detail or "Project title lookup is incomplete; narrow the title or retry"}
           if not complete else {}),
    }
    return result


def _title_matches(
    items: Sequence[MondayProjectBoardItem], project_name: str, *, kind: str,
) -> list[dict[str, Any]]:
    rationale = {
        "exact_title": "Project title matches the request; reviewer confirmation is required",
        "normalized_title": "Project title matches after normalizing case, spacing and punctuation",
        "title_similarity": "Project title shares identifying words or close spellings; similarity is not request intent",
    }[kind]
    return [{**match, "match_kind": kind, "match_rationale": rationale}
            for match in _ordered_matches(items, project_name)]


def _apply_match_limit(result: dict[str, Any]) -> dict[str, Any]:
    matches = result["matches"]
    complete = all((result.get(key) or {}).get("status") not in {"incomplete", "unavailable"}
                   for key in ("titleLookup", "referenceLookup"))
    limited = matches[:PROJECT_MATCH_LIMIT]
    result.update(_result_with_matches(limited))
    result["searchDiagnostics"] = {
        "policyVersion": "project-title-v2",
        "totalQualifiedCandidateCount": len(matches) if complete else None,
        "qualifiedCandidateCountObserved": len(matches),
        "returnedCandidateCount": len(limited), "truncated": len(matches) > len(limited),
    }
    if not complete and not limited:
        result.update(type="unknown", error="Project lookup is incomplete; retry or narrow the request")
    return result


def _match_projects_by_title(
    project_name: str,
    gateway: ProjectMatchingGateway,
    *,
    similarity_threshold: float = 0.55,
) -> dict[str, Any]:
    # Kept in the public signature for existing callers. Character similarity
    # ranks admitted candidates; it cannot substitute for identifying words.
    del similarity_threshold
    budget = MondayProjectSearchBudget()
    normalized = normalize_project_title(project_name)
    if not normalized:
        result = _result_with_matches([])
        result["titleLookup"] = {"status": "not_requested"}
        return result

    exact = gateway.fetch_project_title_candidates(exact_title=project_name.strip(), budget=budget)
    exact_items = tuple(item for item in exact.items
                        if item.state == "active" and normalize_project_title(item.project_title) == normalized)
    if exact_items or not exact.complete:
        return _title_search_result(_title_matches(exact_items, project_name, kind="exact_title"),
            stage="exact_title", budget=budget, complete=exact.complete, detail=exact.reason)

    identifying = _identifying_words(normalized)
    # Generic/numeric-only names can still have normalized whole-title matches.
    # AND keeps that retrieval narrow; such words never admit relaxed matches.
    search_words = identifying or tuple(dict.fromkeys(normalized.split()))
    # A short prefix for long identifiers retrieves nearby spellings in the same
    # query. Local whole-word/near-spelling checks still decide admission.
    retrieval_words = tuple(dict.fromkeys(word[:-2] if identifying and len(word) >= 7 else word
                                         for word in search_words[:MAX_PROJECT_SEARCH_WORDS]))
    candidates = gateway.fetch_project_title_candidates(
        words=retrieval_words, match_all=not identifying, budget=budget)
    items = tuple({item.item_id: item for item in candidates.items if item.state == "active"}.values())
    normalized_items = tuple(item for item in items if normalize_project_title(item.project_title) == normalized)
    if normalized_items:
        return _title_search_result(_title_matches(normalized_items, project_name, kind="normalized_title"),
            stage="normalized_title", budget=budget, complete=candidates.complete, detail=candidates.reason)

    if len(search_words) > MAX_PROJECT_SEARCH_WORDS:
        return _title_search_result([], stage="distinctive_words", budget=budget, complete=False,
            detail="Project title has too many search words; narrow the title and retry")

    requested_tokens = set(normalized.split())
    admitted = [item for item in items
                if _identifying_coverage(identifying, set(normalize_project_title(item.project_title).split()))]
    # Coverage precedes character overlap. Every admitted candidate shares an
    # identifying word or conservative spelling variant; stable ties do not
    # depend on Monday page order.
    def rank(match):
        tokens = set(normalize_project_title(match["title"]).split())
        return (-_identifying_coverage(identifying, tokens), -len(requested_tokens & tokens),
                -match["similarity"], normalize_project_title(match["title"]),
                str(match["name"]), str(match["id"]))

    matches = sorted(_title_matches(admitted, project_name, kind="title_similarity"), key=rank)
    return _title_search_result(matches, stage="distinctive_words" if identifying else "normalized_title",
        budget=budget, complete=candidates.complete, detail=candidates.reason)


def match_projects(
    project_name: str,
    gateway: ProjectMatchingGateway,
    *,
    similarity_threshold: float = 0.55,
    detected_references: Sequence[str | dict[str, Any]] = (),
) -> dict[str, Any]:
    """Combine cited printed identities and title matches without classifying intent.

    Title similarity remains a search score. A match does not establish that the
    client's request is an amendment, nor select a project on the reviewer's behalf.
    Optional reference reads have an explicit unavailable result on failure.
    """
    from ..specification_references import parse_drawing_reference, parse_project_reference

    references = {}
    for detected in detected_references:
        literal = (detected.get("project_reference") or detected.get("full_drawing_reference")
                   or detected.get("literal")) if isinstance(detected, dict) else detected
        reference = parse_project_reference(literal) or parse_drawing_reference(literal)
        if reference is not None:
            references.setdefault(reference.project_key, literal)
    if not references:
        return _apply_match_limit(_match_projects_by_title(
            project_name, gateway, similarity_threshold=similarity_threshold))

    lookup: dict[str, Any] = {"status": "available", "references": list(references), "candidates": []}
    exact_items = ()
    fetch = getattr(gateway, "fetch_project_items_matching_references", None)
    if fetch is None:
        lookup.update(status="unavailable", detail="Direct project reference lookup is unavailable")
    elif len(references) > 32:
        lookup.update(status="unavailable", detail="Too many project references; select a narrower request")
    else:
        try:
            exact_items = fetch(tuple(references))
        except (HTTPException, ValueError):
            lookup.update(status="unavailable", detail="Project reference lookup could not be completed; retry or review candidates")
    # Defend the exact identity boundary even for an alternative gateway.
    exact_matches = []
    for item in exact_items:
        parsed = parse_project_reference(item.project_reference)
        resolved = ([parsed.project_key] if parsed is not None and parsed.project_key in references else [])
        confirmations = []
        if parsed is None:
            for literal in item.reference_confirmations:
                drawing = parse_drawing_reference(literal)
                if (drawing is not None and drawing.project_key in references
                        and item.project_reference.strip() == drawing.project_number):
                    resolved.append(drawing.project_key)
                    confirmations.append(literal)
        if resolved:
            exact_matches.append({**_legacy_match(item, project_name), "match_kind": "exact_reference",
                "match_rationale": ("Project membership is confirmed by an exact-prefix child drawing"
                    if confirmations else "Printed project reference matches the current request"),
                "resolved_project_references": sorted(set(resolved)),
                **({"reference_confirmations": confirmations} if confirmations else {})})
    lookup["candidates"] = [
        match for match in exact_matches
    ]
    if not project_name.strip():
        title_result = _result_with_matches([])
        title_lookup = {"status": "not_requested"}
    else:
        try:
            title_result = _match_projects_by_title(project_name, gateway, similarity_threshold=similarity_threshold)
            title_lookup = title_result["titleLookup"]
        except HTTPException:
            title_result = _result_with_matches([])
            title_lookup = {"status": "unavailable", "detail": "Project title lookup could not be completed; retry or review references"}
    matches_by_id = {
        str(match["id"]): match
        for match in lookup["candidates"] if match["state"] == "active"
    }
    for match in title_result["matches"]:
        matches_by_id.setdefault(str(match["id"]), match)
    result = _result_with_matches(list(matches_by_id.values()))
    result.update(referenceLookup=lookup, titleLookup=title_lookup)
    return _apply_match_limit(result)


def build_matching_contract(
    project_name: str,
    legacy_result: dict[str, Any],
) -> dict[str, Any]:
    candidates = [
        {
            "rank": rank,
            "mondayItemId": str(match["id"]),
            "projectReference": str(match["name"]),
            "projectTitle": str(match["title"]),
            "similarity": float(match["similarity"]),
            "matchPercentage": f"{float(match['similarity']):.1%}",
            "createdDate": match.get("created_date"),
            **({"matchKind": match["match_kind"], "matchRationale": match["match_rationale"]}
               if "match_kind" in match else {}),
            **({"referenceConfirmations": match["reference_confirmations"]}
               if match.get("reference_confirmations") else {}),
            **({"resolvedProjectReferences": match["resolved_project_references"],
                "resolvedProjectReference": (match["resolved_project_references"][0]
                    if len(match["resolved_project_references"]) == 1 else None)}
               if match.get("resolved_project_references") else {}),
        }
        for rank, match in enumerate(legacy_result.get("matches", []), start=1)
    ]
    return {
        "schemaVersion": 2 if "titleLookup" in legacy_result or "referenceLookup" in legacy_result else 1,
        "extractedProjectTitle": project_name,
        "candidateCount": len(candidates),
        "candidates": candidates,
        "parityDiagnostics": {
            "exists": bool(legacy_result.get("exists")),
            "bestMatch": legacy_result.get("best_match"),
        },
        **({"referenceLookup": legacy_result["referenceLookup"]} if "referenceLookup" in legacy_result else {}),
        **({"titleLookup": legacy_result["titleLookup"]} if "titleLookup" in legacy_result else {}),
        **({"searchDiagnostics": legacy_result["searchDiagnostics"]} if "searchDiagnostics" in legacy_result else {}),
    }
