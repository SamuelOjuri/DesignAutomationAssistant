from typing import Any, Optional

import jwt
import requests
from fastapi import HTTPException

from .config import settings

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_OAUTH_URL = "https://auth.monday.com/oauth2/authorize"
MONDAY_TOKEN_URL = "https://auth.monday.com/oauth2/token"

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
    resp = requests.post(
        MONDAY_API_URL,
        json={"query": query, "variables": {"ids": [str(item_id)]}},
        headers={"Authorization": access_token},
        timeout=10,
    )

    if resp.status_code == 401:
        return False
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"monday API error ({resp.status_code})")

    data = resp.json()
    return bool(data.get("data", {}).get("items"))

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
    resp = requests.post(
        MONDAY_API_URL,
        json={"query": ASSET_QUERY, "variables": {"itemIds": [str(item_id)]}},
        headers={"Authorization": access_token},
        timeout=20,
    )
    if resp.status_code == 401:
        raise HTTPException(status_code=403, detail="monday access token invalid")
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"monday API error ({resp.status_code})")

    payload = resp.json()
    if payload.get("errors"):
        raise HTTPException(status_code=502, detail="monday GraphQL error")

    items = payload.get("data", {}).get("items") or []
    if not items:
        raise HTTPException(status_code=404, detail="monday item not found")

    return items[0]

def download_asset(url: str, access_token: Optional[str] = None) -> requests.Response:
    headers = {"Authorization": access_token} if access_token else None
    resp = requests.get(url, headers=headers, stream=True, timeout=60)
    if resp.status_code == 401:
        raise HTTPException(status_code=403, detail="monday asset access denied")
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"monday asset download failed ({resp.status_code})")
    return resp