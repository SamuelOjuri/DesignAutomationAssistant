from __future__ import annotations

import csv
from datetime import datetime
import io
import json
import re
from typing import Any, Mapping, Optional

from .parameter_extraction import CANONICAL_PARAMETER_ORDER


def clean_extracted_value(value: Any) -> Any:
    if not value:
        return value
    value_string = str(value)
    value_string = re.sub(r"^\s*:\s*", "", value_string)
    match = re.search(r"[\"']?\s*:\s*[\"']([^\"']+)[\"']", value_string)
    if match:
        return match.group(1).strip()
    return value_string.strip(" \"',")


def format_date_for_monday(date_string: Any) -> str:
    if not date_string:
        return ""
    cleaned = clean_extracted_value(date_string)
    for date_format in (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d %b %Y",
        "%d %B %Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d/%m/%y",
        "%d-%m-%y",
    ):
        try:
            return datetime.strptime(str(cleaned).strip(), date_format).strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            continue
    return ""


def format_hour_for_monday(time_string: Any) -> Optional[dict[str, int]]:
    if not time_string:
        return None
    cleaned = str(clean_extracted_value(time_string)).strip()
    match = re.match(r"^(\d{1,2})[:\.](\d{2})$", cleaned)
    if match:
        return {"hour": int(match.group(1)), "minute": int(match.group(2))}

    match = re.match(r"^(\d{1,2})[:\.](\d{2})\s*(AM|PM|am|pm)$", cleaned)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = match.group(3).upper()
    if meridiem == "PM" and hour != 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    return {"hour": hour, "minute": minute}


def format_dropdown_for_monday(
    value: Any,
    settings_string: str,
) -> Optional[dict[str, list[Any]]]:
    if not value or not settings_string:
        return None
    try:
        settings = json.loads(settings_string)
    except json.JSONDecodeError:
        return None
    for label in settings.get("labels", []):
        if label.get("name") == value:
            return {"ids": [label.get("id")]}
    return None


def build_ai_data_rows(
    parameters: Mapping[str, Any],
    sources: Optional[Mapping[str, Any]] = None,
) -> list[tuple[str, Any, Any]]:
    if not isinstance(parameters, Mapping) or not parameters:
        return []
    source_values = sources if isinstance(sources, Mapping) else {}
    rows: list[tuple[str, Any, Any]] = []
    seen: set[str] = set()
    for key in CANONICAL_PARAMETER_ORDER:
        if key in parameters:
            rows.append(
                (
                    key,
                    clean_extracted_value(parameters.get(key, "")),
                    source_values.get(key, ""),
                )
            )
            seen.add(key)
    for key in sorted(value for value in parameters if value not in seen):
        rows.append(
            (
                key,
                clean_extracted_value(parameters.get(key, "")),
                source_values.get(key, ""),
            )
        )
    return rows


def build_ai_data_csv_bytes(
    parameters: Mapping[str, Any],
    sources: Optional[Mapping[str, Any]] = None,
) -> bytes:
    rows = build_ai_data_rows(parameters, sources)
    if not rows:
        return b""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["Parameter", "Value", "Source"])
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")
