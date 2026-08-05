from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import PurePath
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence


EMAIL_COLUMN_ID = "file_mkpbm883"
SUPPORTED_EMAIL_EXTENSIONS = frozenset({".eml", ".msg"})


class DesignProcessingInputError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DesignEmailAsset:
    asset_id: str
    filename: str
    file_extension: Optional[str]
    size: int
    created_at: str
    download_url: str
    download_requires_auth: bool

    def revision_record(self) -> dict[str, object]:
        return {
            "assetId": self.asset_id,
            "createdAt": self.created_at,
            "filename": self.filename,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class DesignProcessingTargetSnapshot:
    board_id: str
    item_id: str
    group_id: str
    name: str
    email_assets: tuple[DesignEmailAsset, ...]
    input_revision: Optional[str]

    @property
    def missing_name(self) -> bool:
        return not self.name.strip()

    @property
    def missing_email(self) -> bool:
        return not self.email_assets


class DownloadedAssetLike(Protocol):
    temp_path: str
    content_type: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class DownloadedDesignEmailAsset:
    source: DesignEmailAsset
    temp_path: str
    content_type: str
    content_sha256: str
    size_bytes: int


def is_supported_email_asset(
    filename: Optional[str],
    file_extension: Optional[str],
) -> bool:
    filename_extension = PurePath(filename or "").suffix.lower()
    normalized_extension = (file_extension or "").strip().lower()
    if normalized_extension and not normalized_extension.startswith("."):
        normalized_extension = f".{normalized_extension}"
    return (
        filename_extension in SUPPORTED_EMAIL_EXTENSIONS
        or normalized_extension in SUPPORTED_EMAIL_EXTENSIONS
    )


def canonical_revision_bytes(assets: Sequence[DesignEmailAsset]) -> bytes:
    ordered_assets = sorted(
        assets,
        key=lambda asset: (int(asset.asset_id), asset.filename),
    )
    records = [asset.revision_record() for asset in ordered_assets]
    return json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_design_input_revision(assets: Sequence[DesignEmailAsset]) -> str:
    if not assets:
        raise ValueError("at least one supported Email asset is required")
    return hashlib.sha256(canonical_revision_bytes(assets)).hexdigest()


def download_design_email_assets(
    assets: Sequence[DesignEmailAsset],
    access_token: str,
    *,
    downloader: Optional[
        Callable[[dict[str, Any], str], DownloadedAssetLike]
    ] = None,
) -> tuple[DownloadedDesignEmailAsset, ...]:
    if downloader is None:
        from .storage_ingest import download_asset_to_temp

        downloader = download_asset_to_temp

    downloaded_assets: list[DownloadedDesignEmailAsset] = []
    downloaded_temp_paths: list[str] = []
    try:
        for asset in sorted(assets, key=lambda value: (int(value.asset_id), value.filename)):
            asset_payload: dict[str, Any] = {
                "id": asset.asset_id,
                "name": asset.filename,
                "file_extension": asset.file_extension,
                "file_size": asset.size,
                "created_at": asset.created_at,
                "url": asset.download_url if asset.download_requires_auth else None,
                "public_url": (
                    asset.download_url if not asset.download_requires_auth else None
                ),
            }
            downloaded = downloader(asset_payload, access_token)
            downloaded_temp_paths.append(downloaded.temp_path)
            if downloaded.size_bytes != asset.size:
                raise DesignProcessingInputError(
                    f"Email asset {asset.asset_id} downloaded size does not match metadata"
                )
            digest = downloaded.sha256
            if (
                len(digest) != 64
                or digest.lower() != digest
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise DesignProcessingInputError(
                    f"Email asset {asset.asset_id} download hash is invalid"
                )
            downloaded_assets.append(
                DownloadedDesignEmailAsset(
                    source=asset,
                    temp_path=downloaded.temp_path,
                    content_type=downloaded.content_type,
                    content_sha256=digest,
                    size_bytes=downloaded.size_bytes,
                )
            )
    except Exception:
        for temp_path in downloaded_temp_paths:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise
    return tuple(downloaded_assets)


def parse_design_processing_target(
    item: Mapping[str, Any],
) -> DesignProcessingTargetSnapshot:
    board_id = _nested_required_id(item, "board", context="item")
    group_id = _nested_required_id(item, "group", context="item")
    item_id = _required_decimal_id(item.get("id"), field_name="item.id")
    name_value = item.get("name")
    if name_value is not None and not isinstance(name_value, str):
        raise DesignProcessingInputError("item.name must be a string or null")

    membership = _parse_email_membership(item)
    assets_by_id = _index_asset_metadata(item)
    email_assets: list[DesignEmailAsset] = []

    for member in membership:
        asset_id = _required_decimal_id(
            member.get("assetId"),
            field_name=f"{EMAIL_COLUMN_ID}.files[].assetId",
        )
        metadata = assets_by_id.get(asset_id)
        if metadata is None:
            member_name = member.get("name")
            if isinstance(member_name, str) and PurePath(member_name).suffix:
                if not is_supported_email_asset(member_name, None):
                    continue
            raise DesignProcessingInputError(
                f"Email asset {asset_id} is missing item asset metadata"
            )

        if not is_supported_email_asset(
            _optional_string(metadata.get("name")),
            _optional_string(metadata.get("file_extension")),
        ):
            continue
        email_assets.append(_parse_supported_asset(asset_id, metadata))

    email_assets.sort(key=lambda asset: (int(asset.asset_id), asset.filename))
    if len({asset.asset_id for asset in email_assets}) != len(email_assets):
        raise DesignProcessingInputError("Email membership contains duplicate asset IDs")

    ordered_assets = tuple(email_assets)
    input_revision = (
        compute_design_input_revision(ordered_assets) if ordered_assets else None
    )
    return DesignProcessingTargetSnapshot(
        board_id=board_id,
        item_id=item_id,
        group_id=group_id,
        name=name_value or "",
        email_assets=ordered_assets,
        input_revision=input_revision,
    )


def _parse_email_membership(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    column_values = item.get("column_values")
    if not isinstance(column_values, list):
        raise DesignProcessingInputError("item.column_values must be a list")
    email_columns = [
        value
        for value in column_values
        if isinstance(value, Mapping) and value.get("id") == EMAIL_COLUMN_ID
    ]
    if len(email_columns) != 1:
        raise DesignProcessingInputError(
            f"item must contain exactly one {EMAIL_COLUMN_ID} column value"
        )

    raw_value = email_columns[0].get("value")
    if raw_value is None or raw_value == "":
        return []
    if not isinstance(raw_value, str):
        raise DesignProcessingInputError(f"{EMAIL_COLUMN_ID}.value must be JSON text")
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise DesignProcessingInputError(
            f"{EMAIL_COLUMN_ID}.value is malformed JSON"
        ) from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("files"), list):
        raise DesignProcessingInputError(
            f"{EMAIL_COLUMN_ID}.value must contain a files array"
        )
    files = parsed["files"]
    if not all(isinstance(file_value, Mapping) for file_value in files):
        raise DesignProcessingInputError(
            f"{EMAIL_COLUMN_ID}.files must contain objects"
        )
    return files


def _index_asset_metadata(
    item: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    raw_assets = item.get("assets")
    if not isinstance(raw_assets, list):
        raise DesignProcessingInputError("item.assets must be a list")
    assets: dict[str, Mapping[str, Any]] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, Mapping):
            raise DesignProcessingInputError("item.assets must contain objects")
        asset_id = _required_decimal_id(
            raw_asset.get("id"),
            field_name="item.assets[].id",
        )
        if asset_id in assets:
            raise DesignProcessingInputError(
                f"item.assets contains duplicate asset ID {asset_id}"
            )
        assets[asset_id] = raw_asset
    return assets


def _parse_supported_asset(
    asset_id: str,
    metadata: Mapping[str, Any],
) -> DesignEmailAsset:
    filename = metadata.get("name")
    if not isinstance(filename, str) or not filename:
        raise DesignProcessingInputError(f"Email asset {asset_id} is missing its filename")

    raw_size = metadata.get("file_size")
    if isinstance(raw_size, bool) or not isinstance(raw_size, (int, str)):
        raise DesignProcessingInputError(f"Email asset {asset_id} has an invalid size")
    if isinstance(raw_size, str) and not raw_size.strip().isdecimal():
        raise DesignProcessingInputError(f"Email asset {asset_id} has an invalid size")
    try:
        size = int(raw_size)
    except (TypeError, ValueError) as exc:
        raise DesignProcessingInputError(
            f"Email asset {asset_id} is missing its size"
        ) from exc
    if size < 0:
        raise DesignProcessingInputError(f"Email asset {asset_id} has an invalid size")

    created_at = _normalize_utc_timestamp(metadata.get("created_at"), asset_id)
    public_url = _optional_string(metadata.get("public_url"))
    private_url = _optional_string(metadata.get("url"))
    download_url = public_url or private_url
    if not download_url:
        raise DesignProcessingInputError(
            f"Email asset {asset_id} is missing its download URL"
        )

    return DesignEmailAsset(
        asset_id=asset_id,
        filename=filename,
        file_extension=_optional_string(metadata.get("file_extension")),
        size=size,
        created_at=created_at,
        download_url=download_url,
        download_requires_auth=public_url is None,
    )


def _normalize_utc_timestamp(value: object, asset_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesignProcessingInputError(
            f"Email asset {asset_id} is missing its created_at timestamp"
        )
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DesignProcessingInputError(
            f"Email asset {asset_id} has an invalid created_at timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DesignProcessingInputError(
            f"Email asset {asset_id} created_at must include a timezone"
        )
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _nested_required_id(
    value: Mapping[str, Any],
    key: str,
    *,
    context: str,
) -> str:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise DesignProcessingInputError(f"{context}.{key} metadata is missing")
    raw_id = nested.get("id")
    if raw_id is None or isinstance(raw_id, bool):
        raise DesignProcessingInputError(f"{context}.{key}.id is missing")
    normalized = str(raw_id).strip()
    if not normalized:
        raise DesignProcessingInputError(f"{context}.{key}.id is missing")
    return normalized


def _required_decimal_id(value: object, *, field_name: str) -> str:
    if value is None or isinstance(value, bool):
        raise DesignProcessingInputError(f"{field_name} is missing")
    normalized = str(value).strip()
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise DesignProcessingInputError(f"{field_name} must be a positive decimal ID")
    return str(int(normalized))


def _optional_string(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None