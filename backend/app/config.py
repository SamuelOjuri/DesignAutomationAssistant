from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator  

class Settings(BaseSettings):
    # monday
    monday_client_id: str
    monday_client_secret: str
    monday_signing_secret: str
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