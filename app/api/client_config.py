"""Publishable browser configuration. No service credentials."""

from urllib.parse import urlparse

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, SecretStr

from app.config import get_settings

router = APIRouter(prefix="/api/v1", tags=["client"])
_MAX_ANON_KEY_LENGTH = 4096


class ClientConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supabase_url: str | None
    supabase_anon_key: str | None


def _public_supabase_url(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip().rstrip("/")
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return candidate


def _public_anon_key(value: SecretStr | None) -> str | None:
    if value is None:
        return None
    key = value.get_secret_value().strip()
    if not key or len(key) > _MAX_ANON_KEY_LENGTH or "://" in key:
        return None
    return key


@router.get("/client-config", response_model=ClientConfigResponse)
def client_config() -> ClientConfigResponse:
    settings = get_settings()
    return ClientConfigResponse(
        supabase_url=_public_supabase_url(settings.supabase_url),
        supabase_anon_key=_public_anon_key(settings.supabase_anon_key),
    )
