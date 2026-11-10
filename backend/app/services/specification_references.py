"""Drawing identities and read-only candidate ranking shared by review/extraction.

Parsing establishes structure, never issuer, ownership or applicability. Those
remain checks on the source evidence and the associated Monday project.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping


REFERENCE_PARSER_VERSION = "specification-reference-v1"
_DASHES = str.maketrans(dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"))
_VERSION = r"\d+(?:\s*\.\s*\d+)+"
_NUMBER = r"\d+\s*_\s*" + _VERSION + r"(?:\s*-\s*[A-Z]+)?"
REFERENCE_TEXT_PATTERN = r"(?<!\w)(?:[A-Z]{2}\s*|D\.S\s*-\s*)?" + _NUMBER + r"(?!\w|\.\w)"
_DRAWING = re.compile(
    r"(?P<prefix>[A-Z]{2}\s*|D\.S\s*-\s*)?(?P<project>\d+)\s*_\s*"
    r"(?P<version>" + _VERSION + r")(?:\s*-\s*(?P<revision>[A-Z]+))?", re.IGNORECASE,
)
_PROJECT = re.compile(r"(?P<prefix>[A-Z]{2})\s*(?P<project>\d+)", re.IGNORECASE)
_AMBIGUOUS_SPACED_PREFIXES = frozenset({
    "am", "an", "as", "at", "be", "by", "do", "go", "he", "if", "in", "is",
    "it", "me", "my", "no", "of", "on", "or", "re", "so", "to", "uk", "up", "us", "we",
})
_PROJECT_LABEL = re.compile(
    r"\b(?:ref(?:erence)?|project(?:\s+(?:ref(?:erence)?|number|no\.?))?|"
    r"drawing(?:\s+(?:ref(?:erence)?|number|no\.?))?|scheme|job|regarding)\s*[:#=-]?\s*$", re.IGNORECASE,
)


def normalize_reference_dashes(value):
    return value.translate(_DASHES) if isinstance(value, str) else value


@dataclass(frozen=True, slots=True)
class ProjectReference:
    literal: str
    prefix: str
    project_number: str

    @property
    def key(self) -> str:
        return self.prefix.upper() + self.project_number

    @property
    def project_key(self) -> str:
        return self.key


@dataclass(frozen=True, slots=True)
class DrawingReference:
    literal: str
    prefix: str | None
    project_number: str
    components: tuple[int, ...]
    revision: str | None
    suffix: str

    @property
    def project_key(self) -> str:
        return (self.prefix or "").upper() + self.project_number

    @property
    def key(self) -> str:
        # Keep the actual numeric text (and its leading zeros) in identities.
        return re.sub(r"\s+", "", normalize_reference_dashes(self.literal)).upper()

    @property
    def series_key(self) -> str:
        # First numeric suffix component is the issue year/version. Subsequent
        # components identify the drawing series. Explicit roof scopes are used
        # in addition by candidate ranking; separate series never merge values.
        suffix = re.sub(r"\s+", "", self.suffix).split("-", 1)[0]
        return self.project_key + "_*." + suffix.split(".", 1)[1]

    @property
    def rank(self) -> tuple[tuple[int, ...], int]:
        return self.components, revision_rank(self.revision)


@dataclass(frozen=True, slots=True)
class ReferenceMatch:
    reference: DrawingReference | ProjectReference
    start: int
    end: int


def parse_project_reference(value: str | None) -> ProjectReference | None:
    literal = str(value or "").strip()
    match = _PROJECT.fullmatch(literal)
    return ProjectReference(literal, match["prefix"].upper(), match["project"]) if match else None


def parse_drawing_reference(value: str | None, *, confirmed_legacy: bool = False) -> DrawingReference | None:
    literal = str(value or "").strip()
    normalized = normalize_reference_dashes(literal)
    match = _DRAWING.fullmatch(normalized)
    if not match:
        return None
    prefix = re.sub(r"\s+", "", match["prefix"] or "").rstrip("-").upper() or None
    if (prefix is None or prefix == "D.S") and not confirmed_legacy:
        return None
    return DrawingReference(
        literal, prefix, match["project"],
        tuple(int(part.strip()) for part in match["version"].split(".")),
        match["revision"].upper() if match["revision"] else None,
        normalized.split("_", 1)[1].strip(),
    )


def find_references(text: str, *, include_legacy: bool = False) -> list[ReferenceMatch]:
    """Find full drawings and printed project refs, with literal source offsets.

    Legacy candidates can be inventoried explicitly, but require confirmation
    before being used as drawing identities.
    """
    normalized = normalize_reference_dashes(str(text or ""))
    results = []
    occupied = []
    for match in re.finditer(REFERENCE_TEXT_PATTERN, normalized, re.IGNORECASE):
        reference = parse_drawing_reference(text[match.start():match.end()], confirmed_legacy=include_legacy)
        if reference:
            results.append(ReferenceMatch(reference, match.start(), match.end()))
            occupied.append((match.start(), match.end()))
    for match in re.finditer(r"(?<![\w.@])[A-Z]{2}\s*\d+(?!\w|\s*_)", normalized, re.IGNORECASE):
        if not any(start <= match.start() < end for start, end in occupied):
            # Headers contain ordinary phrases such as "at 15:30", "on 29"
            # and domains ending ".uk 29". A spaced common word is not itself
            # evidence of a printed project reference. An explicit adjacent
            # project/reference label disambiguates it without banning any
            # prefix from the structural parser or compact reference family.
            literal = text[match.start():match.end()]
            line_start = normalized.rfind("\n", 0, match.start()) + 1
            if (re.match(r"[A-Z]{2}\s+", literal, re.IGNORECASE)
                    and literal[:2].casefold() in _AMBIGUOUS_SPACED_PREFIXES
                    and not _PROJECT_LABEL.search(normalized[line_start:match.start()])):
                continue
            reference = parse_project_reference(literal)
            if reference:
                results.append(ReferenceMatch(reference, match.start(), match.end()))
    return sorted(results, key=lambda entry: entry.start)


def drawing_reference_key(reference: str | None) -> str:
    return re.sub(r"\s+", "", normalize_reference_dashes(str(reference or ""))).casefold()


def drawing_references_equal(first: str | None, second: str | None, *,
                             confirmed_project_reference: str | None = None) -> bool:
    """Compare legacy spellings only after explicit project/CRM confirmation.

    Normal keys never alias prefixes. A caller that has verified CRM membership
    may provide that project's printed reference to compare its bare/D.S name.
    """
    first_key, second_key = drawing_reference_key(first), drawing_reference_key(second)
    if not first_key or not second_key:
        return False
    if first_key == second_key:
        return True
    project = parse_project_reference(confirmed_project_reference)
    left = parse_drawing_reference(first, confirmed_legacy=True)
    right = parse_drawing_reference(second, confirmed_legacy=True)
    if not project or not left or not right:
        return False
    return (left.project_number == right.project_number == project.project_number
            and left.prefix in {project.prefix, None, "D.S"}
            and right.prefix in {project.prefix, None, "D.S"}
            and drawing_reference_key(left.suffix) == drawing_reference_key(right.suffix))


def reference_revision(reference: str | None, *, confirmed_legacy: bool = False) -> str | None:
    parsed = parse_drawing_reference(reference, confirmed_legacy=confirmed_legacy)
    return parsed.suffix if parsed else None


def revision_rank(revision: str | None) -> int:
    """Excel-style alphabetic sequence: Y < Z < AA < AB (unrevised = 0)."""
    result = 0
    for letter in str(revision or "").upper():
        if not "A" <= letter <= "Z":
            raise ValueError("Revision must contain only alphabetic characters")
        result = result * 26 + ord(letter) - ord("A") + 1
    return result


def recommend_drawing_candidates(
    drawings: Iterable[Mapping], *, requested_reference: str | None = None,
    project_reference: str | None = None, scope: str | None = None,
    unavailable: bool = False,
) -> dict:
    """Inventory candidates and recommend the latest active drawing per roof.

    An exact active target wins even when historical. Fallbacks remain proposals:
    multiple roofs, duplicate exact identities and unreadable inventories cannot
    silently select a drawing. No column values are merged here.
    """
    if unavailable:
        return {"status": "unavailable", "selectedItemId": None, "candidates": [],
                "recommendations": [], "issues": ["Drawing lookup is unavailable; retry or select a verified source"]}
    project = parse_project_reference(project_reference)
    requested = parse_drawing_reference(requested_reference, confirmed_legacy=True)
    def series_key(reference):
        key = reference.series_key
        if project and reference.prefix in {None, "D.S"} and reference.project_number == project.project_number:
            key = project.key + key[len(reference.project_key):]
        return key
    candidates = []
    groups = {}
    for drawing in drawings:
        reference = parse_drawing_reference(drawing.get("name") or drawing.get("reference"), confirmed_legacy=True)
        state = drawing.get("state")
        board = drawing.get("board") or {}
        candidate_scope = drawing.get("scope") or drawing.get("roof")
        readable = bool(drawing.get("id") and (board.get("id") or drawing.get("boardId"))
                        and state in {"active", "archived", "deleted"} and reference)
        associated = not project or (reference is not None and (
            reference.project_key == project.key or (
                reference.prefix in {None, "D.S"} and reference.project_number == project.project_number)))
        scoped = not scope or not candidate_scope or str(candidate_scope).strip().casefold() == scope.strip().casefold()
        candidate = {
            "itemId": str(drawing.get("id") or drawing.get("itemId") or ""),
            "boardId": str(board.get("id") or drawing.get("boardId") or ""),
            "reference": str(drawing.get("name") or drawing.get("reference") or ""),
            "state": state, "scope": candidate_scope, "readable": readable,
            "applicable": readable and associated and scoped,
        }
        candidates.append(candidate)
        if candidate["applicable"] and state == "active":
            series = str(drawing.get("series") or series_key(reference))
            project_key = (project.key if project and reference.prefix in {None, "D.S"}
                           and reference.project_number == project.project_number else reference.project_key)
            group = (project_key, series, str(candidate_scope or "").strip().casefold())
            groups.setdefault(group, []).append((reference, candidate))
    # Sorting only stabilizes presentation. Monday IDs never decide recency.
    candidates.sort(key=lambda candidate: (candidate["reference"].casefold(), candidate["itemId"]))
    recommendations = []
    for group in sorted(groups):
        entries = groups[group]
        latest = max(reference.rank for reference, _ in entries)
        recommendations.extend({**candidate, "rationale": "Latest active drawing in this roof/drawing series"}
                               for reference, candidate in entries if reference.rank == latest)
    exact = [candidate for candidate in candidates if candidate["applicable"] and candidate["state"] == "active"
             and requested and drawing_references_equal(candidate["reference"], requested.literal,
                                                        confirmed_project_reference=project_reference)]
    issues = []
    selected = None
    if requested_reference and requested is None:
        status = "unmatched"
        issues.append("Requested reference cannot be parsed; verify the existing drawing identity")
    elif len(exact) == 1:
        selected = exact[0]["itemId"]
        status = "exact"
        if not any(candidate["itemId"] == selected for candidate in recommendations):
            issues.append("Requested drawing is historical; newer active alternatives are available")
    elif len(exact) > 1:
        status = "ambiguous"
        issues.append("Several active drawings share the requested reference; select the correct drawing")
    elif recommendations:
        # Retain exact series when a target is present; other roofs stay visible
        # in the candidate inventory but cannot supply its technical values.
        relevant = recommendations
        if requested:
            relevant = [candidate for candidate in recommendations
                        if (parsed := parse_drawing_reference(candidate["reference"], confirmed_legacy=True))
                        and series_key(parsed) == series_key(requested)]
            issues.append("Requested drawing is archived, missing or unavailable; review existing alternatives")
        if len(relevant) == 1:
            selected = relevant[0]["itemId"]
            status = "fallback"
        else:
            status = "ambiguous" if relevant else "unmatched"
            issues.append("Select the relevant roof/drawing; several series may be plausible" if relevant
                          else "No active drawing matches the requested project and series")
    else:
        status = "empty" if not candidates else "no_active"
        issues.append("No drawings were found" if not candidates else "No usable active drawings; archived/deleted or unreadable drawings cannot be selected")
    return {"status": status, "selectedItemId": selected, "candidates": candidates,
            "recommendations": recommendations, "issues": issues}
