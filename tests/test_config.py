from pydantic import SecretStr
from sqlalchemy.engine import make_url

import pytest

from app.config import Settings, get_settings
from app.persistence import (
    DatabaseConfigurationError,
    configured_database_url,
    normalize_database_url,
)
from app.persistence.database import (
    configured_migration_database_url,
    migration_login_is_privileged,
    runtime_database_login,
)


def test_settings_load_without_secrets() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "CommitmentOps AI"
    assert settings.environment == "development"
    assert settings.database_url is None
    assert settings.migration_database_url is None
    assert settings.supabase_url is None
    assert settings.supabase_anon_key is None
    assert settings.supabase_service_role_key is None
    assert settings.llm_api_key is None
    assert settings.llm_api_base_url is None


def test_settings_read_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://db.example.test/commitmentops",
    )
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "test-anon-not-a-real-secret")

    settings = Settings(_env_file=None)

    assert settings.environment == "production"
    assert settings.log_level == "WARNING"
    assert isinstance(settings.database_url, SecretStr)
    assert settings.supabase_url == "https://example.supabase.co"
    assert isinstance(settings.supabase_anon_key, SecretStr)
    assert settings.supabase_anon_key.get_secret_value() == "test-anon-not-a-real-secret"


def test_database_url_is_required_only_for_database_operations() -> None:
    settings = Settings(_env_file=None, database_url=None)

    with pytest.raises(
        DatabaseConfigurationError,
        match="database configuration is unavailable",
    ):
        configured_database_url(settings)


def test_conventional_postgresql_url_uses_psycopg_driver() -> None:
    assert normalize_database_url(
        "postgresql://db.example.test/commitmentops"
    ) == "postgresql+psycopg://db.example.test/commitmentops"


def test_migration_url_prefers_the_migration_login() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://app_login:not-a-secret@db.example/commitmentops",
        migration_database_url=(
            "postgresql://migrator:not-a-secret@db.example/commitmentops"
        ),
    )

    migration = make_url(configured_migration_database_url(settings))
    assert migration.username == "migrator"
    assert runtime_database_login(settings) == "app_login"


def test_sqlite_migration_url_falls_back_to_the_application_url() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///commitmentops-test.db",
    )

    assert (
        configured_migration_database_url(settings)
        == "sqlite+pysqlite:///commitmentops-test.db"
    )
    assert runtime_database_login(settings) is None


def test_postgres_runtime_login_must_be_a_plain_role_name() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://bad-name@db.example/commitmentops",
    )

    with pytest.raises(DatabaseConfigurationError, match="plain role name"):
        runtime_database_login(settings)


def test_underprivileged_migration_login_is_rejected() -> None:
    assert migration_login_is_privileged(
        is_superuser=False,
        can_create_role=False,
    ) is False
    assert migration_login_is_privileged(
        is_superuser=True,
        can_create_role=False,
    ) is True
    assert migration_login_is_privileged(
        is_superuser=False,
        can_create_role=True,
    ) is True


def test_get_settings_is_cached(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()

    first = get_settings()
    second = get_settings()

    assert first is second
    get_settings.cache_clear()
