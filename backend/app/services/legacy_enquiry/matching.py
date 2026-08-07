from __future__ import annotations

from difflib import SequenceMatcher
import itertools
import re
from typing import Any, Protocol, Sequence

from ...monday_client import MondayProjectBoardItem


PROJECT_SEARCH_START_DATE = "2021-01-01"


class ProjectMatchingGateway(Protocol):
    def fetch_project_word_hit_counts(
        self,
        words: Sequence[str],
    ) -> dict[str, int]: ...

    def fetch_project_items_matching_words(
        self,
        words: Sequence[str],
        *,
        start_date: str = PROJECT_SEARCH_START_DATE,
    ) -> tuple[MondayProjectBoardItem, ...]: ...

    def fetch_project_items_matching_full_text(
        self,
        project_name: str,
        *,
        start_date: str = PROJECT_SEARCH_START_DATE,
    ) -> tuple[MondayProjectBoardItem, ...]: ...

    def fetch_active_project_items_since(
        self,
        *,
        start_date: str = PROJECT_SEARCH_START_DATE,
    ) -> tuple[MondayProjectBoardItem, ...]: ...


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
    return sorted(matches, key=lambda match: match["similarity"], reverse=True)


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


def match_projects(
    project_name: str,
    gateway: ProjectMatchingGateway,
    *,
    similarity_threshold: float = 0.55,
) -> dict[str, Any]:
    meaningful_words = filter_meaningful_words(project_name)
    if not meaningful_words:
        items = gateway.fetch_project_items_matching_full_text(
            project_name,
            start_date=PROJECT_SEARCH_START_DATE,
        )
        return _result_with_matches(_ordered_matches(items, project_name))

    counts = gateway.fetch_project_word_hit_counts(meaningful_words)
    ordered_words = sorted(
        meaningful_words,
        key=lambda word: counts.get(word, 1_000_000),
    )
    matches: list[dict[str, Any]] = []
    for subset_size in range(len(ordered_words), 0, -1):
        for subset in itertools.combinations(ordered_words, subset_size):
            items = gateway.fetch_project_items_matching_words(
                subset,
                start_date=PROJECT_SEARCH_START_DATE,
            )
            matches = _ordered_matches(items, project_name)
            if matches:
                return _result_with_matches(matches)

    fallback_items = gateway.fetch_active_project_items_since(
        start_date=PROJECT_SEARCH_START_DATE,
    )
    return _result_with_matches(
        _ordered_matches(
            fallback_items,
            project_name,
            similarity_threshold=similarity_threshold,
        )
    )


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
        }
        for rank, match in enumerate(legacy_result.get("matches", []), start=1)
    ]
    return {
        "schemaVersion": 1,
        "extractedProjectTitle": project_name,
        "candidateCount": len(candidates),
        "candidates": candidates,
        "parityDiagnostics": {
            "exists": bool(legacy_result.get("exists")),
            "bestMatch": legacy_result.get("best_match"),
        },
    }
