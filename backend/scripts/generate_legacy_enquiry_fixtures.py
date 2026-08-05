from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import itertools
import json
import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import extract_msg
import requests

if __package__:
    from .verify_legacy_enquiry_manifest import (
        DEFAULT_LEGACY_ROOT,
        EXPECTED_MANIFEST_DIGEST,
        ManifestVerificationError,
        verify_legacy_manifest,
    )
else:
    from verify_legacy_enquiry_manifest import (
        DEFAULT_LEGACY_ROOT,
        EXPECTED_MANIFEST_DIGEST,
        ManifestVerificationError,
        verify_legacy_manifest,
    )


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_PATH = (
    WORKSPACE_ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "legacy_enquiry"
    / "v1"
    / "input.json"
)
DEFAULT_EXPECTED_PATH = DEFAULT_INPUT_PATH.with_name("expected.json")
DEFAULT_CSV_PATH = DEFAULT_INPUT_PATH.with_name("ai_data.csv")


class FixtureGenerationError(RuntimeError):
    pass


def _load_legacy_definitions(
    source_path: Path,
    names: set[str],
    namespace: dict[str, Any],
) -> dict[str, Any]:
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    selected_nodes: list[ast.stmt] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            selected_nodes.append(node)
            continue
        if isinstance(node, ast.Assign):
            assigned_names = {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
            if assigned_names & names:
                selected_nodes.append(node)

    module = ast.Module(body=selected_nodes, type_ignores=[])
    compiled = compile(module, str(source_path), "exec")
    exec(compiled, namespace)

    missing = names.difference(namespace)
    if missing:
        raise FixtureGenerationError(
            f"Pinned legacy source {source_path} is missing definitions: {sorted(missing)}"
        )
    return namespace


def _run_synchronously(items, callback, **_kwargs):
    return [callback(*item) for item in items]


def _load_fixture_input(input_path: Path) -> dict[str, Any]:
    try:
        fixture_input = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureGenerationError(
            f"Unable to load fixture input {input_path}: {exc}"
        ) from exc

    if fixture_input.get("fixtureVersion") != 1:
        raise FixtureGenerationError("Unsupported legacy fixture input version")
    return fixture_input


def _verified_source_asset(fixture_input: dict[str, Any]) -> Path:
    source_asset = fixture_input["sourceEmail"]
    source_path = WORKSPACE_ROOT / Path(source_asset["path"])
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise FixtureGenerationError(
            f"Unable to read fixture source email {source_path}: {exc}"
        ) from exc

    actual_digest = hashlib.sha256(source_bytes).hexdigest()
    if actual_digest != source_asset["sha256"]:
        raise FixtureGenerationError(
            f"Fixture source email hash mismatch: expected {source_asset['sha256']}, "
            f"got {actual_digest}"
        )
    if len(source_bytes) != source_asset["sizeBytes"]:
        raise FixtureGenerationError(
            f"Fixture source email size mismatch: expected {source_asset['sizeBytes']}, "
            f"got {len(source_bytes)}"
        )
    return source_path


def _load_email_functions(legacy_root: Path, fixture_input: dict[str, Any]):
    extraction_text = fixture_input["recordedResponses"]["attachmentExtractionText"]
    namespace: dict[str, Any] = {
        "BinaryIO": BinaryIO,
        "BytesParser": BytesParser,
        "Dict": Dict,
        "List": List,
        "Tuple": Tuple,
        "Union": Union,
        "ZoneInfo": ZoneInfo,
        "datetime": datetime,
        "extract_msg": extract_msg,
        "io": io,
        "logger": logging.getLogger("legacy_fixture.email_extraction"),
        "parsedate_to_datetime": parsedate_to_datetime,
        "policy": policy,
        "process_image_with_gemini": lambda *_args, **_kwargs: extraction_text,
        "process_items_in_parallel": _run_synchronously,
        "process_pdf_batch": lambda _items: extraction_text,
        "process_pdf_with_gemini": lambda *_args, **_kwargs: extraction_text,
        "should_batch_pdfs": lambda _items: True,
        "time": __import__("time"),
    }
    return _load_legacy_definitions(
        legacy_root / "backend" / "app" / "utils" / "email_extraction.py",
        {
            "extract_text_from_email",
            "format_email_date",
            "is_inline_attachment",
            "is_inline_image",
            "process_email_content",
        },
        namespace,
    )


def _load_parameter_functions(legacy_root: Path, fixture_input: dict[str, Any]):
    helper_namespace = _load_legacy_definitions(
        legacy_root / "backend" / "app" / "utils" / "helpers.py",
        {"map_tapered_insulation_value"},
        {},
    )
    responses = fixture_input["recordedResponses"]

    def query_llm(_content: str, query: str) -> str:
        if query:
            return responses["parameterExtraction"]
        return responses["projectTitle"]

    return _load_legacy_definitions(
        legacy_root / "backend" / "app" / "services" / "parameter_extraction.py",
        {"extract_parameters", "extract_project_name_from_content"},
        {
            "map_tapered_insulation_value": helper_namespace[
                "map_tapered_insulation_value"
            ],
            "query_llm": query_llm,
            "re": re,
        },
    )


def _load_csv_functions(legacy_root: Path):
    return _load_legacy_definitions(
        legacy_root / "backend" / "app" / "routes" / "monday.py",
        {
            "CANONICAL_PARAM_ORDER",
            "build_ai_data_csv_bytes",
            "clean_extracted_value",
        },
        {
            "Any": Any,
            "Dict": Dict,
            "Optional": Optional,
            "csv": csv,
            "io": io,
            "re": re,
        },
    )


def _load_matching_class(legacy_root: Path):
    return _load_legacy_definitions(
        legacy_root
        / "backend"
        / "app"
        / "utils"
        / "monday_dot_com_interface.py",
        {"MondayDotComInterface"},
        {
            "BinaryIO": BinaryIO,
            "Optional": Optional,
            "SequenceMatcher": SequenceMatcher,
            "current_app": SimpleNamespace(debug=False),
            "itertools": itertools,
            "json": json,
            "requests": requests,
        },
    )["MondayDotComInterface"]


def _matching_output(legacy_root: Path, fixture_input: dict[str, Any]) -> dict[str, Any]:
    matching_input = fixture_input["matching"]
    interface_class = _load_matching_class(legacy_root)
    interface = interface_class("fixture-token")

    def execute_query(query: str, _variables=None):
        if "w0: boards" in query:
            words = re.findall(r'compare_value: \["([^"]+)"\]', query)
            counts = matching_input["wordHitCounts"]
            return {
                "data": {
                    f"w{index}": [
                        {"items_page": {"items": [{}] * int(counts.get(word, 0))}}
                    ]
                    for index, word in enumerate(words)
                }
            }
        return {
            "data": {
                "boards": [
                    {"items_page": {"cursor": None, "items": matching_input["items"]}}
                ]
            }
        }

    interface.execute_query = execute_query
    return interface.check_project_exists(matching_input["projectName"])


def generate_fixture(
    input_path: Path = DEFAULT_INPUT_PATH,
    legacy_root: Path = DEFAULT_LEGACY_ROOT,
) -> tuple[bytes, bytes]:
    verify_legacy_manifest(legacy_root=legacy_root)
    fixture_input = _load_fixture_input(input_path)
    source_path = _verified_source_asset(fixture_input)

    email_functions = _load_email_functions(legacy_root, fixture_input)
    source_bytes = source_path.read_bytes()
    header, body, attachments, inline_images = email_functions[
        "process_email_content"
    ](source_bytes, source_path.name)
    email_text = f"{header}\n{body}"
    all_text = email_functions["extract_text_from_email"](
        email_text,
        attachments,
        inline_images,
    )

    parameter_functions = _load_parameter_functions(legacy_root, fixture_input)
    parameters = parameter_functions["extract_parameters"](all_text)
    project_name = parameter_functions["extract_project_name_from_content"](
        email_text,
        all_text,
    )

    sources = {parameter: "Email Content" for parameter in parameters}
    csv_functions = _load_csv_functions(legacy_root)
    csv_bytes = csv_functions["build_ai_data_csv_bytes"](parameters, sources)

    expected = {
        "fixtureVersion": 1,
        "legacyManifestDigest": EXPECTED_MANIFEST_DIGEST,
        "sourceEmail": {
            "path": fixture_input["sourceEmail"]["path"],
            "sha256": fixture_input["sourceEmail"]["sha256"],
            "sizeBytes": len(source_bytes),
        },
        "extraction": {
            "allTextSha256": hashlib.sha256(all_text.encode("utf-8")).hexdigest(),
            "attachmentFilenames": [item["filename"] for item in attachments],
            "emailHeader": header,
            "inlineImageFilenames": [item["filename"] for item in inline_images],
            "parameters": parameters,
            "projectName": project_name,
        },
        "matching": _matching_output(legacy_root, fixture_input),
    }
    expected_bytes = (
        json.dumps(expected, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return expected_bytes, csv_bytes


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate or verify versioned golden fixtures from pinned legacy code."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--legacy-root", type=Path, default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED_PATH)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write regenerated fixture outputs instead of checking committed bytes.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        expected_bytes, csv_bytes = generate_fixture(
            input_path=args.input.resolve(),
            legacy_root=args.legacy_root.resolve(),
        )
        outputs = {
            args.expected.resolve(): expected_bytes,
            args.csv.resolve(): csv_bytes,
        }
        if args.write:
            for path, content in outputs.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            print(f"Generated {len(outputs)} legacy enquiry fixture files.")
            return 0

        mismatches = [
            path for path, content in outputs.items() if not path.is_file() or path.read_bytes() != content
        ]
        if mismatches:
            raise FixtureGenerationError(
                "Committed legacy fixtures are missing or stale: "
                + ", ".join(str(path) for path in mismatches)
            )
    except (FixtureGenerationError, ManifestVerificationError) as exc:
        print(f"Legacy enquiry fixture generation failed: {exc}")
        return 1

    print("Legacy enquiry fixtures reproduce byte-for-byte.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())