from supabase import create_client, ClientOptions
from .config import settings

supabase = create_client(
    settings.supabase_url,
    settings.supabase_service_role_key,
    options=ClientOptions(
        post_timeout=300,  # Increase timeout to 5 minutes
    ),
)