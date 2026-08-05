from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


EXPECTED_MANIFEST_DIGEST = (
    "82d5612a9efce97660c3a3fef36a731d45597cb3096e58365865727ba719e28e"
)
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEGACY_ROOT = WORKSPACE_ROOT / "producer" / "TechnicalDesignAssistant"
DEFAULT_MANIFEST_PATH = (
    WORKSPACE_ROOT
    / "backend"
    / "app"
    / "services"
    / "legacy_enquiry"
    / "legacy_manifest.json"
)


class ManifestVerificationError(RuntimeError):
    pass


def _load_canonical_manifest(manifest_path: Path) -> tuple[list[dict[str, str]], bytes]:
    try:
        raw_manifest = manifest_path.read_bytes()
    except OSError as exc:
        raise ManifestVerificationError(
            f"Unable to read legacy manifest {manifest_path}: {exc}"
        ) from exc

    try:
        manifest: Any = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestVerificationError(
            f"Legacy manifest is not valid UTF-8 JSON: {exc}"
        ) from exc

    if not isinstance(manifest, list) or not manifest:
        raise ManifestVerificationError("Legacy manifest must be a non-empty JSON array")

    entries: list[dict[str, str]] = []
    for index, entry in enumerate(manifest):
        if not isinstance(entry, dict) or list(entry) != ["path", "sha256"]:
            raise ManifestVerificationError(
                f"Manifest entry {index} must contain only path and sha256 in that order"
            )
        path = entry["path"]
        sha256 = entry["sha256"]
        if not isinstance(path, str) or not isinstance(sha256, str):
            raise ManifestVerificationError(
                f"Manifest entry {index} path and sha256 must be strings"
            )
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ManifestVerificationError(
                f"Manifest entry {index} has an invalid lowercase SHA-256"
            )
        entries.append({"path": path, "sha256": sha256})

    paths = [entry["path"] for entry in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ManifestVerificationError(
            "Legacy manifest paths must be unique and lexicographically ordered"
        )

    canonical_bytes = json.dumps(
        entries,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if raw_manifest.rstrip(b"\r\n") != canonical_bytes:
        raise ManifestVerificationError(
            "Legacy manifest must use canonical JSON with no insignificant whitespace"
        )

    digest = hashlib.sha256(canonical_bytes).hexdigest()
    if digest != EXPECTED_MANIFEST_DIGEST:
        raise ManifestVerificationError(
            f"Legacy manifest digest mismatch: expected {EXPECTED_MANIFEST_DIGEST}, got {digest}"
        )

    return entries, canonical_bytes


def verify_legacy_manifest(
    legacy_root: Path = DEFAULT_LEGACY_ROOT,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> list[Path]:
    entries, _ = _load_canonical_manifest(manifest_path)
    verified_paths: list[Path] = []

    for entry in entries:
        source_path = legacy_root / Path(entry["path"])
        try:
            actual_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ManifestVerificationError(
                f"Unable to read pinned legacy file {source_path}: {exc}"
            ) from exc

        if actual_digest != entry["sha256"]:
            raise ManifestVerificationError(
                f"Legacy file hash mismatch for {entry['path']}: "
                f"expected {entry['sha256']}, got {actual_digest}"
            )
        verified_paths.append(source_path)

    return verified_paths


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the pinned legacy enquiry source snapshot."
    )
    parser.add_argument(
        "--legacy-root",
        type=Path,
        default=DEFAULT_LEGACY_ROOT,
        help="Path to the local TechnicalDesignAssistant legacy repository.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Path to the tracked canonical legacy manifest.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        verified_paths = verify_legacy_manifest(
            legacy_root=args.legacy_root.resolve(),
            manifest_path=args.manifest.resolve(),
        )
    except ManifestVerificationError as exc:
        print(f"Legacy enquiry manifest verification failed: {exc}")
        return 1

    print(
        f"Verified {len(verified_paths)} legacy enquiry files "
        f"(manifest {EXPECTED_MANIFEST_DIGEST})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())