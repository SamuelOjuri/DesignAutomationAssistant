from typing import Any, Optional, Sequence

import jwt
import requests
from fastapi import HTTPException
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import settings

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_OAUTH_URL = "https://auth.monday.com/oauth2/authorize"
MONDAY_TOKEN_URL = "https://auth.monday.com/oauth2/token"
TRANSIENT_MONDAY_STATUS_CODES = {429, 500, 502, 503, 504}


class TransientMondayAPIError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"monday API error ({status_code})")


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
    except TransientMondayAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
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

def verify_session_token(session_token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            session_token,
            settings.monday_signing_secret,
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
                items { id }
            }
        }
    }
}
"""

NEXT_ITEMS_PAGE_QUERY = """
query ($cursor: String!, $limit: Int!) {
    next_items_page(cursor: $cursor, limit: $limit) {
        cursor
        items { id }
    }
}
"""


def list_item_ids_in_groups(
    access_token: str,
    board_id: str,
    group_ids: Sequence[str],
    *,
    limit: int = 500,
) -> dict[str, list[str]]:
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

    result: dict[str, list[str]] = {}
    for group in boards[0].get("groups") or []:
        group_id = str(group.get("id"))
        items_page = group.get("items_page") or {}
        item_ids = [str(item.get("id")) for item in items_page.get("items") or [] if item.get("id")]
        cursor = items_page.get("cursor")

        while cursor:
            next_payload = monday_graphql_request(
                access_token,
                NEXT_ITEMS_PAGE_QUERY,
                {"cursor": cursor, "limit": limit},
                timeout=20,
            )
            next_page = next_payload.get("data", {}).get("next_items_page") or {}
            item_ids.extend(str(item.get("id")) for item in next_page.get("items") or [] if item.get("id"))
            cursor = next_page.get("cursor")

        result[group_id] = item_ids
    return result


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


def download_asset(url: str, access_token: Optional[str] = None) -> requests.Response:
    headers = {
        "Accept": "*/*",
        "User-Agent": "DesignAutomationAssistant/1.0",  # Add User-Agent
    }
    if access_token:
        headers["Authorization"] = access_token
    resp = requests.get(url, headers=headers, stream=True, timeout=60)
    if resp.status_code == 401:
        raise HTTPException(status_code=403, detail="monday asset access denied")
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"monday asset download failed ({resp.status_code})")
    return resp