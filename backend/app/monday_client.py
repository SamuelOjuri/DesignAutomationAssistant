from dataclasses import dataclass
from datetime import date
import json
from pathlib import PurePath
from typing import Any, Mapping, Optional, Sequence

import jwt
import requests
from fastapi import HTTPException
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import settings

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_FILE_API_URL = "https://api.monday.com/v2/file"
MONDAY_OAUTH_URL = "https://auth.monday.com/oauth2/authorize"
MONDAY_TOKEN_URL = "https://auth.monday.com/oauth2/token"
TRANSIENT_MONDAY_STATUS_CODES = {429, 500, 502, 503, 504}
DESIGN_PROCESSING_SCALAR_COLUMN_IDS = frozenset(
    {"date_mkpb23av", "hour_mkpbb3j1", "dropdown_mkpbafca"}
)


class TransientMondayAPIError(HTTPException):
    def __init__(
        self,
        upstream_status_code: Optional[int] = None,
        *,
        detail: Optional[str] = None,
    ):
        self.upstream_status_code = upstream_status_code
        if detail is None:
            detail = f"monday API error ({upstream_status_code})"
        super().__init__(status_code=502, detail=detail)


class MondayReadContractError(TransientMondayAPIError):
    pass


class MondayWriteContractError(ValueError):
    pass


def _require_design_scalar_columns(column_values: Mapping[str, Any]) -> None:
    unknown = set(column_values) - DESIGN_PROCESSING_SCALAR_COLUMN_IDS
    if unknown:
        raise MondayWriteContractError(
            "design processing cannot write scalar columns: "
            + ", ".join(sorted(unknown))
        )
    invalid = [
        column_id
        for column_id, value in column_values.items()
        if value is None or value == ""
    ]
    if invalid:
        raise MondayWriteContractError(
            "design processing cannot clear scalar columns: "
            + ", ".join(sorted(invalid))
        )


def _require_design_file_column(column_id: str) -> None:
    if column_id not in DESIGN_PROCESSING_FILE_COLUMN_IDS:
        raise MondayWriteContractError(
            f"design processing cannot access file column {column_id!r}"
        )


def _require_decimal_identifier(value: str, *, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise MondayWriteContractError(
            f"{field_name} must be a positive decimal ID"
        )
    return normalized


def _is_transient_monday_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            TransientMondayAPIError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ),
    )


def monday_headers(access_token: str) -> dict[str, str]:
    headers = {"Authorization": access_token}
    if settings.monday_api_version:
        headers["API-Version"] = settings.monday_api_version
    return headers


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception(_is_transient_monday_error),
    reraise=True,
)
def _post_monday_graphql(
    access_token: str,
    query: str,
    variables: Optional[dict[str, Any]],
    *,
    timeout: int,
) -> requests.Response:
    resp = requests.post(
        MONDAY_API_URL,
        json={"query": query, "variables": variables or {}},
        headers=monday_headers(access_token),
        timeout=timeout,
    )
    if resp.status_code in TRANSIENT_MONDAY_STATUS_CODES:
        raise TransientMondayAPIError(resp.status_code)
    return resp


def monday_graphql_request(
    access_token: str,
    query: str,
    variables: Optional[dict[str, Any]] = None,
    *,
    timeout: int = 10,
    allow_unauthorized: bool = False,
) -> Optional[dict[str, Any]]:
    try:
        resp = _post_monday_graphql(
            access_token,
            query,
            variables,
            timeout=timeout,
        )
    except TransientMondayAPIError:
        raise
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise TransientMondayAPIError(
            detail=f"monday API request failed: {exc}",
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"monday API request failed: {exc}")

    if resp.status_code == 401 and allow_unauthorized:
        return None
    if resp.status_code == 401:
        raise HTTPException(status_code=403, detail="monday access token invalid")
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"monday API error ({resp.status_code})")

    payload = resp.json()
    if payload.get("errors"):
        raise HTTPException(status_code=502, detail="monday GraphQL error")
    return payload


DESIGN_PROCESSING_COLUMN_UPDATE_MUTATION = """
mutation ($boardId: ID!, $itemId: ID!, $columnValues: JSON!) {
    change_multiple_column_values(
        board_id: $boardId
        item_id: $itemId
        column_values: $columnValues
    ) { id }
}
"""


def update_design_owned_columns(
    access_token: str,
    board_id: str,
    item_id: str,
    column_values: Mapping[str, Any],
) -> None:
    values = dict(column_values)
    _require_design_scalar_columns(values)
    if not values:
        return
    normalized_board_id = _require_decimal_identifier(
        board_id,
        field_name="board_id",
    )
    normalized_item_id = _require_decimal_identifier(
        item_id,
        field_name="item_id",
    )
    payload = monday_graphql_request(
        access_token,
        DESIGN_PROCESSING_COLUMN_UPDATE_MUTATION,
        {
            "boardId": normalized_board_id,
            "itemId": normalized_item_id,
            "columnValues": json.dumps(
                values,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
        timeout=20,
    )
    changed = ((payload or {}).get("data") or {}).get(
        "change_multiple_column_values"
    )
    if not isinstance(changed, Mapping) or str(changed.get("id")) != normalized_item_id:
        raise TransientMondayAPIError(
            detail="monday scalar-column update response is incomplete"
        )


def upload_design_file(
    access_token: str,
    item_id: str,
    column_id: str,
    filename: str,
    content: bytes,
    content_type: str,
) -> "MondayFileColumnAsset":
    _require_design_file_column(column_id)
    normalized_item_id = _require_decimal_identifier(
        item_id,
        field_name="item_id",
    )
    if not filename or PurePath(filename).name != filename:
        raise MondayWriteContractError("filename must be a basename")
    if not content:
        raise MondayWriteContractError("design artifact content must not be empty")

    query = (
        "mutation ($file: File!) { "
        f"add_file_to_column(item_id: {normalized_item_id}, "
        f'column_id: "{column_id}", file: $file) '
        "{ id name file_size created_at } }"
    )
    response = requests.post(
        MONDAY_FILE_API_URL,
        data={"query": query},
        files={"variables[file]": (filename, content, content_type)},
        headers=monday_headers(access_token),
        timeout=60,
    )
    if response.status_code in TRANSIENT_MONDAY_STATUS_CODES:
        raise TransientMondayAPIError(response.status_code)
    if not response.ok:
        raise HTTPException(
            status_code=502,
            detail=f"monday file upload error ({response.status_code})",
        )
    payload = response.json()
    if payload.get("errors"):
        raise HTTPException(status_code=502, detail="monday file upload GraphQL error")
    uploaded = ((payload.get("data") or {}).get("add_file_to_column") or {})
    asset_id = uploaded.get("id")
    uploaded_name = uploaded.get("name")
    if not asset_id or uploaded_name != filename:
        raise TransientMondayAPIError(
            detail="monday file upload response is incomplete"
        )
    size = uploaded.get("file_size")
    return MondayFileColumnAsset(
        asset_id=str(asset_id),
        filename=uploaded_name,
        size_bytes=(int(size) if size is not None else None),
        created_at=(
            uploaded.get("created_at")
            if isinstance(uploaded.get("created_at"), str)
            else None
        ),
    )


DESIGN_PROCESSING_UPDATE_ASSETS_MUTATION = """
mutation (
    $boardId: ID!
    $itemId: ID!
    $columnId: String!
    $files: [FileInput!]!
) {
    update_assets_on_item(
        board_id: $boardId
        item_id: $itemId
        column_id: $columnId
        files: $files
    ) { id }
}
"""


def delete_design_file(
    access_token: str,
    board_id: str,
    item_id: str,
    column_id: str,
    asset_id: str,
) -> None:
    _require_design_file_column(column_id)
    normalized_board_id = _require_decimal_identifier(
        board_id,
        field_name="board_id",
    )
    normalized_item_id = _require_decimal_identifier(
        item_id,
        field_name="item_id",
    )
    normalized_asset_id = _require_decimal_identifier(
        asset_id,
        field_name="asset_id",
    )
    current_assets = inspect_design_processing_file_columns(
        access_token,
        normalized_item_id,
    )[column_id]
    matching = [
        asset for asset in current_assets if asset.asset_id == normalized_asset_id
    ]
    if not matching:
        return
    if len(matching) != 1:
        raise MondayWriteContractError(
            f"monday returned duplicate asset {normalized_asset_id!r}"
        )
    retained_assets = [
        asset for asset in current_assets if asset.asset_id != normalized_asset_id
    ]
    if not retained_assets:
        raise MondayWriteContractError(
            "design processing cannot remove the final file from a column"
        )
    files = [
        {
            "assetId": _require_decimal_identifier(
                asset.asset_id,
                field_name="retained asset_id",
            ),
            "fileType": "asset",
            "name": asset.filename,
        }
        for asset in retained_assets
    ]
    payload = monday_graphql_request(
        access_token,
        DESIGN_PROCESSING_UPDATE_ASSETS_MUTATION,
        {
            "boardId": normalized_board_id,
            "itemId": normalized_item_id,
            "columnId": column_id,
            "files": files,
        },
        timeout=20,
    )
    updated = ((payload or {}).get("data") or {}).get("update_assets_on_item")
    if not isinstance(updated, Mapping) or str(updated.get("id")) != normalized_item_id:
        raise TransientMondayAPIError(
            detail="monday retained-asset update response is incomplete"
        )


DESIGN_PROCESSING_SCALAR_COLUMNS_QUERY = """
query ($boardIds: [ID!]) {
    boards(ids: $boardIds) {
        id
        columns(ids: ["date_mkpb23av", "hour_mkpbb3j1", "dropdown_mkpbafca"]) {
            id
            settings_str
        }
    }
}
"""


def fetch_design_owned_column_settings(
    access_token: str,
    board_id: str,
) -> dict[str, str]:
    normalized_board_id = _require_decimal_identifier(
        board_id,
        field_name="board_id",
    )
    payload = monday_graphql_request(
        access_token,
        DESIGN_PROCESSING_SCALAR_COLUMNS_QUERY,
        {"boardIds": [normalized_board_id]},
        timeout=20,
    )
    boards = ((payload or {}).get("data") or {}).get("boards") or []
    if len(boards) != 1:
        raise MondayReadContractError(
            detail="monday design-processing board columns are unavailable"
        )
    columns = boards[0].get("columns") or []
    settings_by_id = {
        str(column.get("id")): (
            column.get("settings_str")
            if isinstance(column.get("settings_str"), str)
            else ""
        )
        for column in columns
        if isinstance(column, Mapping)
        and str(column.get("id")) in DESIGN_PROCESSING_SCALAR_COLUMN_IDS
    }
    if set(settings_by_id) != DESIGN_PROCESSING_SCALAR_COLUMN_IDS:
        raise MondayReadContractError(
            detail="monday design-processing scalar columns are incomplete"
        )
    return settings_by_id

def verify_session_token(session_token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            session_token,
            settings.monday_client_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid monday session token")

def can_read_item(access_token: str, item_id: str) -> bool:
    query = "query ($ids: [ID!]) { items (ids: $ids) { id } }"
    data = monday_graphql_request(
        access_token,
        query,
        {"ids": [str(item_id)]},
        timeout=10,
        allow_unauthorized=True,
    )
    if data is None:
        return False
    return bool(data.get("data", {}).get("items"))

CURRENT_ACCOUNT_QUERY = """
query {
    me {
        account { id }
    }
}
"""


def fetch_current_account_id(access_token: str) -> str:
    payload = monday_graphql_request(
        access_token,
        CURRENT_ACCOUNT_QUERY,
        timeout=10,
    )
    account_id = (
        ((payload.get("data") or {}).get("me") or {})
        .get("account") or {}
    ).get("id")
    if not account_id:
        raise HTTPException(status_code=502, detail="monday account id not found")
    return str(account_id)

ASSET_QUERY = """
query ($itemIds: [ID!]) {
  items(ids: $itemIds) {
    id
    name
    updated_at
    assets {
      id
      name
      file_extension
      file_size
      url
      public_url
      created_at
    }
    column_values {
      column { title }
      id
      type
      value
      text
      ... on FormulaValue { display_value }
      ... on MirrorValue { display_value }
    }
    updates {
      id
      assets {
        id
        name
        file_extension
        file_size
        url
        public_url
        created_at
      }
    }
  }
}
"""

def fetch_item_with_assets(access_token: str, item_id: str) -> dict[str, Any]:
    payload = monday_graphql_request(
        access_token,
        ASSET_QUERY,
        {"itemIds": [str(item_id)]},
        timeout=20,
    )

    items = payload.get("data", {}).get("items") or []
    if not items:
        raise HTTPException(status_code=404, detail="monday item not found")

    return items[0]


ITEM_METADATA_QUERY = """
query ($itemIds: [ID!]) {
    items(ids: $itemIds) {
        id
        name
        updated_at
        board { id name }
        group { id title }
    }
}
"""


def fetch_item_metadata(access_token: str, item_id: str) -> dict[str, Any]:
    account_id = fetch_current_account_id(access_token)
    payload = monday_graphql_request(
        access_token,
        ITEM_METADATA_QUERY,
        {"itemIds": [str(item_id)]},
        timeout=10,
    )
    items = payload.get("data", {}).get("items") or []
    if not items:
        raise HTTPException(status_code=404, detail="monday item not found")
    item = items[0]
    item["account_id"] = account_id
    return item


BOARD_GROUPS_QUERY = """
query ($boardIds: [ID!]) {
    boards(ids: $boardIds) {
        id
        groups { id title }
    }
}
"""


def fetch_board_group_metadata(access_token: str, board_id: str) -> list[dict[str, Any]]:
    payload = monday_graphql_request(
        access_token,
        BOARD_GROUPS_QUERY,
        {"boardIds": [str(board_id)]},
        timeout=10,
    )
    boards = payload.get("data", {}).get("boards") or []
    if not boards:
        raise HTTPException(status_code=404, detail="monday board not found")
    return boards[0].get("groups") or []


GROUP_ITEMS_QUERY = """
query ($boardIds: [ID!], $groupIds: [String!], $limit: Int!) {
    boards(ids: $boardIds) {
        groups(ids: $groupIds) {
            id
            title
            items_page(limit: $limit) {
                cursor
                items { id created_at }
            }
        }
    }
}
"""

NEXT_ITEMS_PAGE_QUERY = """
query ($cursor: String!, $limit: Int!) {
    next_items_page(cursor: $cursor, limit: $limit) {
        cursor
        items { id created_at }
    }
}
"""


@dataclass(frozen=True, slots=True)
class MondayGroupItem:
    item_id: str
    created_at: Optional[str]


def list_items_in_groups(
    access_token: str,
    board_id: str,
    group_ids: Sequence[str],
    *,
    limit: int = 500,
) -> dict[str, list[MondayGroupItem]]:
    if not group_ids:
        return {}

    payload = monday_graphql_request(
        access_token,
        GROUP_ITEMS_QUERY,
        {
            "boardIds": [str(board_id)],
            "groupIds": [str(group_id) for group_id in group_ids],
            "limit": limit,
        },
        timeout=20,
    )
    boards = payload.get("data", {}).get("boards") or []
    if not boards:
        raise HTTPException(status_code=404, detail="monday board not found")

    result: dict[str, list[MondayGroupItem]] = {}
    for group in boards[0].get("groups") or []:
        group_id = str(group.get("id"))
        items_page = group.get("items_page") or {}
        items = [
            MondayGroupItem(
                item_id=str(item.get("id")),
                created_at=(
                    item.get("created_at")
                    if isinstance(item.get("created_at"), str)
                    else None
                ),
            )
            for item in items_page.get("items") or []
            if item.get("id")
        ]
        cursor = items_page.get("cursor")

        while cursor:
            next_payload = monday_graphql_request(
                access_token,
                NEXT_ITEMS_PAGE_QUERY,
                {"cursor": cursor, "limit": limit},
                timeout=20,
            )
            next_page = next_payload.get("data", {}).get("next_items_page") or {}
            items.extend(
                MondayGroupItem(
                    item_id=str(item.get("id")),
                    created_at=(
                        item.get("created_at")
                        if isinstance(item.get("created_at"), str)
                        else None
                    ),
                )
                for item in next_page.get("items") or []
                if item.get("id")
            )
            cursor = next_page.get("cursor")

        result[group_id] = items
    return result


def list_item_ids_in_groups(
    access_token: str,
    board_id: str,
    group_ids: Sequence[str],
    *,
    limit: int = 500,
) -> dict[str, list[str]]:
    return {
        group_id: [item.item_id for item in items]
        for group_id, items in list_items_in_groups(
            access_token,
            board_id,
            group_ids,
            limit=limit,
        ).items()
    }


SOURCE_REVISION_INPUTS_QUERY = """
query ($itemIds: [ID!]) {
    items(ids: $itemIds) {
        id
        name
        updated_at
        board { id name }
        group { id title }
        assets {
            id
            name
            file_extension
            file_size
            created_at
            url
            public_url
        }
        column_values {
            column { title }
            id
            type
            value
            text
            ... on FormulaValue { display_value }
            ... on MirrorValue { display_value }
        }
        updates {
            id
            assets {
                id
                name
                file_extension
                file_size
                created_at
                url
                public_url
            }
        }
    }
}
"""


def fetch_current_source_revision_inputs(
    access_token: str,
    item_id: str,
    *,
    account_id: Optional[str] = None,
) -> dict[str, Any]:
    if account_id is None:
        account_id = fetch_current_account_id(access_token)
    payload = monday_graphql_request(
        access_token,
        SOURCE_REVISION_INPUTS_QUERY,
        {"itemIds": [str(item_id)]},
        timeout=20,
    )
    items = payload.get("data", {}).get("items") or []
    if not items:
        raise HTTPException(status_code=404, detail="monday item not found")
    item = items[0]
    item["account_id"] = account_id
    return item


DESIGN_PROCESSING_EMAIL_COLUMN_ID = "file_mkpbm883"
DESIGN_PROCESSING_FILE_COLUMN_IDS = ("file_mkza7y37", "file_mm59rntf")
PROJECT_TITLE_COLUMN_ID = "text3__1"
PROJECT_CREATED_DATE_COLUMN_ID = "date9__1"

DESIGN_PROCESSING_INTAKE_QUERY = """
query ($itemIds: [ID!]) {
    items(ids: $itemIds) {
        id
        name
        board { id }
        group { id }
        assets {
            id
            name
            file_extension
            file_size
            created_at
            url
            public_url
        }
        column_values(ids: ["file_mkpbm883"]) {
            id
            type
            value
        }
    }
}
"""


def fetch_design_processing_intake_item(
    access_token: str,
    item_id: str,
) -> dict[str, Any]:
    payload = monday_graphql_request(
        access_token,
        DESIGN_PROCESSING_INTAKE_QUERY,
        {"itemIds": [str(item_id)]},
        timeout=20,
    )
    return _extract_single_read_item(payload, context="design-processing intake")


@dataclass(frozen=True, slots=True)
class MondayFileColumnAsset:
    asset_id: str
    filename: str
    size_bytes: Optional[int]
    created_at: Optional[str]


DESIGN_PROCESSING_FILE_COLUMNS_QUERY = """
query ($itemIds: [ID!]) {
    items(ids: $itemIds) {
        id
        assets {
            id
            name
            file_size
            created_at
        }
        column_values(ids: ["file_mkza7y37", "file_mm59rntf"]) {
            id
            type
            value
        }
    }
}
"""


def inspect_design_processing_file_columns(
    access_token: str,
    item_id: str,
) -> dict[str, tuple[MondayFileColumnAsset, ...]]:
    payload = monday_graphql_request(
        access_token,
        DESIGN_PROCESSING_FILE_COLUMNS_QUERY,
        {"itemIds": [str(item_id)]},
        timeout=20,
    )
    item = _extract_single_read_item(payload, context="design-processing files")
    raw_assets = item.get("assets")
    raw_columns = item.get("column_values")
    if not isinstance(raw_assets, list) or not isinstance(raw_columns, list):
        raise MondayReadContractError(
            detail="monday design-processing file response is incomplete"
        )

    assets_by_id: dict[str, Mapping[str, Any]] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, Mapping):
            raise MondayReadContractError(
                detail="monday design-processing asset metadata is malformed"
            )
        asset_id = _normalize_monday_asset_id(raw_asset.get("id"))
        if asset_id in assets_by_id:
            raise MondayReadContractError(
                detail=f"monday returned duplicate asset metadata for {asset_id}"
            )
        assets_by_id[asset_id] = raw_asset

    columns_by_id = {
        str(column.get("id")): column
        for column in raw_columns
        if isinstance(column, Mapping)
        and str(column.get("id")) in DESIGN_PROCESSING_FILE_COLUMN_IDS
    }
    if set(columns_by_id) != set(DESIGN_PROCESSING_FILE_COLUMN_IDS):
        raise MondayReadContractError(
            detail="monday design-processing file columns are incomplete"
        )

    result: dict[str, tuple[MondayFileColumnAsset, ...]] = {}
    for column_id in DESIGN_PROCESSING_FILE_COLUMN_IDS:
        members = _parse_file_column_membership(
            columns_by_id[column_id].get("value"),
            column_id=column_id,
        )
        column_assets: list[MondayFileColumnAsset] = []
        seen_ids: set[str] = set()
        for member in members:
            asset_id = _normalize_monday_asset_id(member.get("assetId"))
            if asset_id in seen_ids:
                raise MondayReadContractError(
                    detail=f"monday {column_id} contains duplicate asset {asset_id}"
                )
            seen_ids.add(asset_id)
            metadata = assets_by_id.get(asset_id)
            if metadata is None:
                raise MondayReadContractError(
                    detail=f"monday {column_id} asset {asset_id} is missing metadata"
                )
            filename = metadata.get("name")
            if not isinstance(filename, str) or not filename:
                raise MondayReadContractError(
                    detail=f"monday {column_id} asset {asset_id} has no filename"
                )
            column_assets.append(
                MondayFileColumnAsset(
                    asset_id=asset_id,
                    filename=filename,
                    size_bytes=_optional_nonnegative_int(metadata.get("file_size")),
                    created_at=(
                        metadata.get("created_at")
                        if isinstance(metadata.get("created_at"), str)
                        else None
                    ),
                )
            )
        result[column_id] = tuple(column_assets)
    return result


@dataclass(frozen=True, slots=True)
class MondayProjectBoardItem:
    item_id: str
    project_reference: str
    project_title: str
    state: str
    created_date: Optional[str]


PROJECT_ITEMS_PAGE_FIELDS = """
cursor
items {
    id
    name
    state
    column_values(ids: ["text3__1", "date9__1"]) {
        id
        text
        ... on MirrorValue { display_value }
    }
}
"""

NEXT_PROJECT_ITEMS_PAGE_QUERY = f"""
query ($cursor: String!, $limit: Int!) {{
    next_items_page(cursor: $cursor, limit: $limit) {{
        {PROJECT_ITEMS_PAGE_FIELDS}
    }}
}}
"""


def fetch_project_word_hit_counts(
    access_token: str,
    board_id: str,
    words: Sequence[str],
) -> dict[str, int]:
    normalized_words = _normalize_project_words(words)
    if not normalized_words:
        return {}
    normalized_board_id = _normalize_positive_decimal_id(board_id, "board_id")
    blocks = []
    for index, word in enumerate(normalized_words):
        blocks.append(
            f"""
            w{index}: boards(ids: [{normalized_board_id}]) {{
                items_page(
                    query_params: {{
                        rules: [{{
                            column_id: "{PROJECT_TITLE_COLUMN_ID}"
                            compare_value: [{json.dumps(word)}]
                            operator: contains_text
                        }}]
                    }}
                    limit: 50
                ) {{
                    items {{ id }}
                }}
            }}
            """
        )
    payload = monday_graphql_request(
        access_token,
        "query {" + "".join(blocks) + "}",
        timeout=20,
    )
    data = _require_mapping(payload, "data", context="project word counts")
    counts: dict[str, int] = {}
    for index, word in enumerate(normalized_words):
        boards = data.get(f"w{index}")
        if not isinstance(boards, list) or not boards or not isinstance(boards[0], Mapping):
            raise MondayReadContractError(
                detail="monday project word-count response is incomplete"
            )
        page = boards[0].get("items_page")
        if not isinstance(page, Mapping) or not isinstance(page.get("items"), list):
            raise MondayReadContractError(
                detail="monday project word-count page is incomplete"
            )
        counts[word] = len(page["items"])
    return counts


def fetch_project_items_matching_words(
    access_token: str,
    board_id: str,
    words: Sequence[str],
    *,
    start_date: str = "2021-01-01",
    limit: int = 500,
) -> tuple[MondayProjectBoardItem, ...]:
    normalized_words = _normalize_project_words(words)
    if not normalized_words:
        raise ValueError("at least one project search word is required")
    return _fetch_project_items_by_title_terms(
        access_token,
        board_id,
        normalized_words,
        start_date=start_date,
        limit=limit,
        combine_with_and=True,
        context="project word search",
    )


def fetch_project_items_matching_full_text(
    access_token: str,
    board_id: str,
    project_name: str,
    *,
    start_date: str = "2021-01-01",
    limit: int = 500,
) -> tuple[MondayProjectBoardItem, ...]:
    normalized_project_name = str(project_name).strip()
    if not normalized_project_name:
        raise ValueError("project_name must not be empty")
    return _fetch_project_items_by_title_terms(
        access_token,
        board_id,
        (normalized_project_name,),
        start_date=start_date,
        limit=limit,
        combine_with_and=False,
        context="project full-text search",
    )


def _fetch_project_items_by_title_terms(
    access_token: str,
    board_id: str,
    words: Sequence[str],
    *,
    start_date: str,
    limit: int,
    combine_with_and: bool,
    context: str,
) -> tuple[MondayProjectBoardItem, ...]:
    normalized_board_id = _normalize_positive_decimal_id(board_id, "board_id")
    _validate_project_start_date(start_date)
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")

    rules = [
        f"""
        {{
            column_id: "{PROJECT_TITLE_COLUMN_ID}"
            compare_value: [{json.dumps(word)}]
            operator: contains_text
        }}
        """
        for word in words
    ]
    rules.append(
        f"""
        {{
            column_id: "{PROJECT_CREATED_DATE_COLUMN_ID}"
            compare_value: ["EXACT", {json.dumps(start_date)}]
            operator: greater_than_or_equals
        }}
        """
    )
    operator = "operator: and" if combine_with_and else ""
    query = f"""
    query {{
        boards(ids: [{normalized_board_id}]) {{
            items_page(
                query_params: {{
                    rules: [{','.join(rules)}]
                    {operator}
                }}
                limit: {limit}
            ) {{
                {PROJECT_ITEMS_PAGE_FIELDS}
            }}
        }}
    }}
    """
    payload = monday_graphql_request(access_token, query, timeout=20)
    page = _extract_project_board_page(payload, context=context)
    return _parse_project_items(page)


def fetch_active_project_items_since(
    access_token: str,
    board_id: str,
    *,
    start_date: str = "2021-01-01",
    limit: int = 500,
) -> tuple[MondayProjectBoardItem, ...]:
    normalized_board_id = _normalize_positive_decimal_id(board_id, "board_id")
    _validate_project_start_date(start_date)
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    query = f"""
    query {{
        boards(ids: [{normalized_board_id}]) {{
            items_page(
                query_params: {{
                    rules: [{{
                        column_id: "{PROJECT_CREATED_DATE_COLUMN_ID}"
                        compare_value: ["EXACT", {json.dumps(start_date)}]
                        operator: greater_than_or_equals
                    }}]
                }}
                limit: {limit}
            ) {{
                {PROJECT_ITEMS_PAGE_FIELDS}
            }}
        }}
    }}
    """
    payload = monday_graphql_request(access_token, query, timeout=20)
    page = _extract_project_board_page(payload, context="project fallback search")
    items = list(_parse_project_items(page))
    cursor = page.get("cursor")
    while cursor:
        if not isinstance(cursor, str):
            raise MondayReadContractError(
                detail="monday project page cursor is malformed"
            )
        next_payload = monday_graphql_request(
            access_token,
            NEXT_PROJECT_ITEMS_PAGE_QUERY,
            {"cursor": cursor, "limit": limit},
            timeout=20,
        )
        next_data = _require_mapping(
            next_payload,
            "data",
            context="next project page",
        )
        next_page = next_data.get("next_items_page")
        if not isinstance(next_page, Mapping):
            raise MondayReadContractError(
                detail="monday next project page is incomplete"
            )
        items.extend(_parse_project_items(next_page))
        cursor = next_page.get("cursor")
    return tuple(item for item in items if item.state == "active")


def _extract_single_read_item(
    payload: Optional[dict[str, Any]],
    *,
    context: str,
) -> dict[str, Any]:
    data = _require_mapping(payload, "data", context=context)
    items = data.get("items")
    if not isinstance(items, list):
        raise MondayReadContractError(detail=f"monday {context} response is incomplete")
    if not items:
        raise HTTPException(status_code=404, detail="monday item not found")
    if len(items) != 1 or not isinstance(items[0], dict):
        raise MondayReadContractError(detail=f"monday {context} item is malformed")
    return items[0]


def _require_mapping(
    payload: Optional[dict[str, Any]],
    key: str,
    *,
    context: str,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise MondayReadContractError(detail=f"monday {context} response is missing")
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise MondayReadContractError(detail=f"monday {context} response is incomplete")
    return value


def _parse_file_column_membership(
    raw_value: object,
    *,
    column_id: str,
) -> list[Mapping[str, Any]]:
    if raw_value is None or raw_value == "":
        return []
    if not isinstance(raw_value, str):
        raise MondayReadContractError(detail=f"monday {column_id} value is malformed")
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise MondayReadContractError(
            detail=f"monday {column_id} value is malformed"
        ) from exc
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("files"), list):
        raise MondayReadContractError(detail=f"monday {column_id} files are malformed")
    members = parsed["files"]
    if not all(isinstance(member, Mapping) for member in members):
        raise MondayReadContractError(detail=f"monday {column_id} files are malformed")
    return members


def _extract_project_board_page(
    payload: Optional[dict[str, Any]],
    *,
    context: str,
) -> Mapping[str, Any]:
    data = _require_mapping(payload, "data", context=context)
    boards = data.get("boards")
    if not isinstance(boards, list) or not boards or not isinstance(boards[0], Mapping):
        raise MondayReadContractError(detail=f"monday {context} board is missing")
    page = boards[0].get("items_page")
    if not isinstance(page, Mapping) or not isinstance(page.get("items"), list):
        raise MondayReadContractError(detail=f"monday {context} page is incomplete")
    return page


def _parse_project_items(
    page: Mapping[str, Any],
) -> tuple[MondayProjectBoardItem, ...]:
    raw_items = page.get("items")
    if not isinstance(raw_items, list):
        raise MondayReadContractError(detail="monday project items are malformed")
    items: list[MondayProjectBoardItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise MondayReadContractError(detail="monday project item is malformed")
        item_id = _normalize_monday_asset_id(raw_item.get("id"))
        name = raw_item.get("name")
        state = raw_item.get("state")
        column_values = raw_item.get("column_values")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(state, str)
            or not isinstance(column_values, list)
        ):
            raise MondayReadContractError(detail="monday project item is incomplete")
        title = name
        created_date: Optional[str] = None
        for column in column_values:
            if not isinstance(column, Mapping):
                raise MondayReadContractError(
                    detail="monday project column value is malformed"
                )
            text = column.get("text")
            if column.get("id") == PROJECT_TITLE_COLUMN_ID and isinstance(text, str) and text:
                title = text
            elif (
                column.get("id") == PROJECT_CREATED_DATE_COLUMN_ID
                and isinstance(text, str)
                and text
            ):
                created_date = text
        items.append(
            MondayProjectBoardItem(
                item_id=item_id,
                project_reference=name,
                project_title=title,
                state=state,
                created_date=created_date,
            )
        )
    return tuple(items)


def _normalize_monday_asset_id(value: object) -> str:
    return _normalize_positive_decimal_id(value, "asset ID")


def _normalize_positive_decimal_id(value: object, field_name: str) -> str:
    if value is None or isinstance(value, bool):
        raise MondayReadContractError(detail=f"monday {field_name} is missing")
    normalized = str(value).strip()
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise MondayReadContractError(detail=f"monday {field_name} is malformed")
    return str(int(normalized))


def _optional_nonnegative_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MondayReadContractError(detail="monday asset size is malformed")
    if isinstance(value, str) and not value.strip().isdecimal():
        raise MondayReadContractError(detail="monday asset size is malformed")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise MondayReadContractError(detail="monday asset size is malformed") from exc
    if normalized < 0:
        raise MondayReadContractError(detail="monday asset size is malformed")
    return normalized


def _normalize_project_words(words: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for word in words:
        value = str(word).strip()
        if value:
            normalized.append(value)
    return tuple(normalized)


def _validate_project_start_date(value: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("start_date must use YYYY-MM-DD format") from exc
    if parsed.isoformat() != value:
        raise ValueError("start_date must use YYYY-MM-DD format")


def download_asset(url: str, access_token: Optional[str] = None) -> requests.Response:
    headers = {
        "Accept": "*/*",
        "User-Agent": "DesignAutomationAssistant/1.0",  # Add User-Agent
    }
    if access_token:
        headers["Authorization"] = access_token
    resp = requests.get(url, headers=headers, stream=True, timeout=60)
    if resp.status_code == 401:
        resp.close()
        raise HTTPException(status_code=403, detail="monday asset access denied")
    if resp.status_code in TRANSIENT_MONDAY_STATUS_CODES:
        resp.close()
        raise TransientMondayAPIError(
            resp.status_code,
            detail=f"monday asset download failed ({resp.status_code})",
        )
    if not resp.ok:
        resp.close()
        raise HTTPException(status_code=502, detail=f"monday asset download failed ({resp.status_code})")
    return resp