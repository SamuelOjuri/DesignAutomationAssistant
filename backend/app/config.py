from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # monday
    monday_client_id: str
    monday_client_secret: str
    monday_signing_secret: str
    monday_oauth_redirect_uri: Optional[str] = None

    # URLs
    main_app_base_url: str = "http://localhost:3000"
    backend_base_url: str = "https://design-automation-assistant-api.onrender.com"

    # Supabase / main-app auth
    supabase_jwt_secret: str  # used to validate main app JWTs

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

settings = Settings()