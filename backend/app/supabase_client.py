from supabase import Client, ClientOptions, create_client
from .config import settings


def create_supabase_client(url: str, key: str, *, options: ClientOptions | None = None) -> Client:
    client = create_client(url, key, options=options)
    # The SDK builds /storage/v1 without the slash even when the base URL has
    # one. Normalize this derived URL before the storage client is lazily built.
    client.storage_url = client.storage_url.with_path(client.storage_url.path.rstrip("/") + "/")
    return client


supabase = create_supabase_client(
    settings.supabase_url,
    settings.supabase_service_role_key,
)
