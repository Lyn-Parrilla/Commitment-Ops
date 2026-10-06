from pydantic import SecretStr

from app.config import Settings, get_settings


def test_settings_load_without_secrets() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "CommitmentOps AI"
    assert settings.environment == "development"
    assert settings.supabase_url is None
    assert settings.supabase_anon_key is None
    assert settings.supabase_service_role_key is None
    assert settings.llm_api_key is None
    assert settings.llm_api_base_url is None


def test_settings_read_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "test-anon-not-a-real-secret")

    settings = Settings(_env_file=None)

    assert settings.environment == "production"
    assert settings.log_level == "WARNING"
    assert settings.supabase_url == "https://example.supabase.co"
    assert isinstance(settings.supabase_anon_key, SecretStr)
    assert settings.supabase_anon_key.get_secret_value() == "test-anon-not-a-real-secret"


def test_get_settings_is_cached(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()

    first = get_settings()
    second = get_settings()

    assert first is second
    get_settings.cache_clear()
