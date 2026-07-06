from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator  

class Settings(BaseSettings):
    # monday
    monday_client_id: str
    monday_client_secret: str
    monday_signing_secret: str
    monday_webhook_shared_secret: Optional[str] = None
    monday_oauth_redirect_uri: Optional[str] = None

    # URLs
    main_app_base_url: str = "https://design-automation-assistant.netlify.app"
    backend_base_url: str = "https://design-automation-assistant-api.onrender.com"

    # Supabase / main-app auth
    supabase_jwt_secret: str  # used to validate main app JWTs
    supabase_jwks_url: str

    # Supabase
    supabase_url: str
    supabase_service_role_key: str
    supabase_storage_bucket: str = "raw-monday"

    # Gemini
    gemini_api_key: str
    gemini_model: str = "gemini-3-flash-preview"

    # Postgres
    database_url: str

    # Auto-sync foundation
    auto_sync_enabled: bool = False
    auto_sync_board_id: str = "1882196103"
    auto_sync_active_group_ids: str = "topics,group_mkpbs35c,group_mkqbx92r"
    auto_sync_excluded_group_ids: str = "group_mkpbd6vy"
    auto_sync_completed_group_id: str = "group_mkpbb3tx"
    auto_sync_retention_days: int = 30
    auto_sync_debounce_seconds: int = 90
    auto_sync_backfill_batch_size: int = 10
    auto_sync_worker_enabled: bool = False
    auto_sync_reconciliation_enabled: bool = False
    auto_sync_purge_enabled: bool = False
    monday_ingestion_access_token: Optional[str] = None
    monday_api_version: str = "2025-04"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("supabase_url", mode="before")
    def _ensure_supabase_url_trailing_slash(cls, v: str) -> str:
        if isinstance(v, str) and v and not v.endswith("/"):
            return v + "/"
        return v

settings = Settings()