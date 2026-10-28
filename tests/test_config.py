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
from app.persistence.rls import (
    verify_configured_runtime_session,
    verify_runtime_session,
)


def test_settings_load_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "APP_NAME",
        "ENVIRONMENT",
        "DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "LLM_API_KEY",
        "LLM_API_BASE_URL",
        "EVIDENCE_STORAGE_ENDPOINT",
        "EVIDENCE_STORAGE_REGION",
        "EVIDENCE_STORAGE_BUCKET",
        "EVIDENCE_STORAGE_ACCESS_KEY_ID",
        "EVIDENCE_STORAGE_SECRET_ACCESS_KEY",
        "EVIDENCE_MAX_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)

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
    assert settings.evidence_storage_endpoint is None
    assert settings.evidence_storage_region is None
    assert settings.evidence_storage_bucket is None
    assert settings.evidence_storage_access_key_id is None
    assert settings.evidence_storage_secret_access_key is None
    assert settings.evidence_max_bytes == 10 * 1024 * 1024


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


_PROJECT_REF = "abcdefghijklmnopqrst"
_SUPABASE_PROJECT_URL = f"https://{_PROJECT_REF}.supabase.co"


def test_supabase_pooler_username_returns_the_custom_role() -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql://commitmentops_runtime."
            f"{_PROJECT_REF}:not-a-secret@aws-0-us-west-1.pooler.supabase.com"
            ":5432/postgres"
        ),
        supabase_url=_SUPABASE_PROJECT_URL,
    )

    assert runtime_database_login(settings) == "commitmentops_runtime"


def test_supabase_pooler_username_rejects_a_different_project() -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql://commitmentops_runtime."
            "zzzzzzzzzzzzzzzzzzzz@pooler.supabase.com/postgres"
        ),
        supabase_url=_SUPABASE_PROJECT_URL,
    )

    with pytest.raises(DatabaseConfigurationError, match="does not match"):
        runtime_database_login(settings)


def test_malformed_pooler_usernames_are_rejected() -> None:
    urls = (
        "postgresql://bad-name.abcdefghijklmnopqrst@db.example/commitmentops",
        "postgresql://commitmentops_runtime.abcdefghijklmnopqrst.extra@db.example/db",
        "postgresql://commitmentops_runtime.@db.example/commitmentops",
        "postgresql://.abcdefghijklmnopqrst@db.example/commitmentops",
        "postgresql://@db.example/commitmentops",
    )
    for database_url in urls:
        settings = Settings(
            _env_file=None,
            database_url=database_url,
            supabase_url=_SUPABASE_PROJECT_URL,
        )
        with pytest.raises(DatabaseConfigurationError, match="plain role name"):
            runtime_database_login(settings)


def test_pooler_username_without_a_configured_project_is_rejected() -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql://commitmentops_runtime."
            f"{_PROJECT_REF}@db.example/commitmentops"
        ),
    )

    with pytest.raises(DatabaseConfigurationError, match="does not match"):
        runtime_database_login(settings)


def test_runtime_preflight_rejects_a_pooler_shaped_name_without_querying() -> None:
    with pytest.raises(DatabaseConfigurationError, match="plain role name"):
        verify_runtime_session(object(), "commitmentops_runtime.abcdefghijklmnopqrst")


def test_configured_runtime_preflight_does_not_open_sqlite(monkeypatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("preflight opened a connection")

    monkeypatch.setattr("app.persistence.rls.create_database_engine", fail)
    settings = Settings(_env_file=None, database_url="sqlite:///unused.db")

    with pytest.raises(DatabaseConfigurationError, match="plain role name"):
        verify_configured_runtime_session(settings)


def test_underprivileged_migration_login_is_rejected() -> None:
    assert migration_login_is_privileged(
        is_superuser=False,
        can_create_role=False,
        bypasses_rls=False,
    ) is False
    assert migration_login_is_privileged(
        is_superuser=True,
        can_create_role=False,
        bypasses_rls=False,
    ) is True
    assert migration_login_is_privileged(
        is_superuser=False,
        can_create_role=True,
        bypasses_rls=True,
    ) is True
    assert migration_login_is_privileged(
        is_superuser=False,
        can_create_role=True,
        bypasses_rls=False,
    ) is False


def test_get_settings_is_cached(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()

    first = get_settings()
    second = get_settings()

    assert first is second
    get_settings.cache_clear()
