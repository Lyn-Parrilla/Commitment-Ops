"""Environment-backed application settings.

Secrets are read from the process environment (or a local .env file) and
must never be hardcoded.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration supplied through environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "CommitmentOps AI"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    supabase_url: str | None = None
    supabase_anon_key: SecretStr | None = None
    supabase_service_role_key: SecretStr | None = None

    llm_api_key: SecretStr | None = None
    llm_api_base_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return cached settings. Clear the cache in tests after env changes."""

    return Settings()
