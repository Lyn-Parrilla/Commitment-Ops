"""SQLAlchemy engine and session construction.

Callers supply the database URL explicitly. Application startup does not connect
to a database, and tests can use isolated SQLite URLs without consulting
environment configuration.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings, get_settings
from app.persistence.exceptions import DatabaseConfigurationError, PersistenceError


class Base(DeclarativeBase):
    """Base for database-specific persistence records."""


SessionFactory = sessionmaker[Session]

_ISOLATED_TEST_SCHEMA = re.compile(r"^commitmentops_test_[0-9a-f]{16}$")
_DENIED_SCHEMAS = frozenset(
    {
        "public",
        "auth",
        "storage",
        "extensions",
        "graphql",
        "graphql_public",
        "realtime",
        "supabase_migrations",
        "information_schema",
        "pg_catalog",
        "pg_toast",
        "vault",
    }
)
_DATABASE_URL = re.compile(
    r"(?i)\b(?:[a-z][a-z0-9+.-]*)://[^\s'\"<>]+"
)
_ROLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SUPABASE_PROJECT_REF = re.compile(r"^[a-z0-9]{20}$")
RUNTIME_LOGIN_ATTRIBUTE = "commitmentops_runtime_login"
_CONNINFO_SECRET = re.compile(
    r"(?i)\b(password|pwd|user|username|host|hostname|dbname|database)"
    r"\s*=\s*(?:'[^']*'|\"[^\"]*\"|\S+)"
)
_SERVER_AT = re.compile(r'(?i)server at "[^"]+"')


def create_database_engine(
    database_url: str,
    *,
    schema: str | None = None,
    connect_args: dict[str, object] | None = None,
) -> Engine:
    """Create a PostgreSQL- or SQLite-compatible SQLAlchemy engine.

    Engine creation does not connect. ``schema`` is only for an isolated
    PostgreSQL test schema and is rejected for every other target.
    """

    normalized_url = normalize_database_url(database_url)
    merged_connect_args = dict(connect_args or {})
    validated_schema: str | None = None
    if schema is not None:
        validated_schema = validate_isolated_test_schema(schema)
        if not make_url(normalized_url).drivername.startswith("postgresql"):
            raise DatabaseConfigurationError(
                "isolated test schemas are only supported on PostgreSQL"
            )
        merged_connect_args["options"] = f"-csearch_path={validated_schema}"
    engine = create_engine(
        normalized_url,
        hide_parameters=True,
        connect_args=merged_connect_args,
    )
    try:
        if validated_schema is not None:
            _install_isolated_schema(engine, validated_schema)
        elif engine.dialect.name == "sqlite":
            event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    except Exception:
        engine.dispose()
        raise
    return engine


def create_session_factory(engine: Engine) -> SessionFactory:
    """Create sessions with domain-friendly post-commit object behavior."""

    return sessionmaker(bind=engine, expire_on_commit=False)


def configured_migration_database_url(settings: Settings | None = None) -> str:
    """Return the URL Alembic should open.

    ``MIGRATION_DATABASE_URL`` wins. An empty migration URL falls back to
    ``DATABASE_URL`` so SQLite tests and a local single-login database keep
    working. On PostgreSQL that fallback is not permission to migrate:
    ``migrations/env.py`` refuses the connection unless the login is a
    superuser or has ``CREATEROLE``.
    """

    active = settings or get_settings()
    migration = active.migration_database_url
    if migration is not None and migration.get_secret_value().strip():
        return normalize_database_url(migration.get_secret_value())
    return configured_database_url(active)


def runtime_database_login(settings: Settings | None = None) -> str | None:
    """Return the PostgreSQL role named by ``DATABASE_URL``.

    SQLite and an unset URL have no login. A direct username must already be
    a plain role name. A Supabase Session Pooler username
    ``<role>.<project-ref>`` is accepted only when ``<project-ref>`` is the
    project in ``SUPABASE_URL``. The returned name is that role, which still
    has to pass the catalog privilege checks. A pooler username is not
    rewritten to ``postgres``.
    """

    active = settings or get_settings()
    configured = active.database_url
    if configured is None or not configured.get_secret_value().strip():
        return None
    parsed = make_url(normalize_database_url(configured.get_secret_value()))
    if not parsed.drivername.startswith("postgresql"):
        return None
    username = parsed.username
    if username is not None and _ROLE_NAME.fullmatch(username):
        return username
    role_name, separator, project_ref = (username or "").partition(".")
    pooler_username = (
        separator == "."
        and "." not in project_ref
        and _ROLE_NAME.fullmatch(role_name) is not None
        and _SUPABASE_PROJECT_REF.fullmatch(project_ref) is not None
    )
    if pooler_username and _supabase_project_ref(active.supabase_url) == project_ref:
        return role_name
    if pooler_username:
        raise DatabaseConfigurationError(
            "pooler project reference does not match the configured "
            "Supabase project"
        )
    raise DatabaseConfigurationError(
        "runtime database login is missing or not a plain role name"
    )


def _supabase_project_ref(url: str | None) -> str | None:
    """Return the project reference from a hosted Supabase project URL."""

    if url is None or not url.strip():
        return None
    parsed = urlparse(url.strip())
    hostname = parsed.hostname
    if parsed.scheme.lower() != "https" or hostname is None:
        return None
    suffix = ".supabase.co"
    if not hostname.endswith(suffix):
        return None
    project_ref = hostname[: -len(suffix)]
    if _SUPABASE_PROJECT_REF.fullmatch(project_ref) is None:
        return None
    return project_ref


def migration_login_is_privileged(
    *,
    is_superuser: bool,
    can_create_role: bool,
) -> bool:
    """True when a migration login can create or administer cluster roles."""

    return bool(is_superuser or can_create_role)


def configured_database_url(settings: Settings | None = None) -> str:
    """Read the required URL without exposing it in configuration reprs."""

    configured = (settings or get_settings()).database_url
    if configured is None or not configured.get_secret_value().strip():
        raise DatabaseConfigurationError(
            "database configuration is unavailable"
        )
    return configured.get_secret_value()


def normalize_database_url(database_url: str) -> str:
    """Use psycopg 3 for conventional PostgreSQL provider URLs."""

    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
    return database_url


def resolve_migration_url(explicit_url: str | None) -> str | None:
    """Return an explicit migration URL, or None when the app URL is required.

    An empty value does not count as explicit. Callers must not substitute
    ``DATABASE_URL`` until this returns None.
    """

    if explicit_url and explicit_url.strip():
        return explicit_url.strip()
    return None


def validate_isolated_test_schema(schema: str) -> str:
    """Accept only a newly generated CommitmentOps test schema name."""

    if (
        not isinstance(schema, str)
        or schema.lower() in _DENIED_SCHEMAS
        or _ISOLATED_TEST_SCHEMA.fullmatch(schema) is None
    ):
        raise DatabaseConfigurationError(
            "refusing a database schema that is not an isolated test schema"
        )
    return schema


def quoted_isolated_schema(schema: str) -> str:
    """Return a quoted identifier after the isolated-schema check."""

    return f'"{validate_isolated_test_schema(schema)}"'


def isolated_schema_is_current(current: object, expected: str) -> bool:
    """True only when ``current`` is exactly the validated isolated schema.

    ``None``, a different schema, and any non-string result are inconclusive
    and therefore not a match.
    """

    if not isinstance(current, str) or current != expected:
        return False
    try:
        validate_isolated_test_schema(expected)
    except DatabaseConfigurationError:
        return False
    return True


def release_inspection_transaction(connection: object) -> None:
    """Commit a schema-inspection read so Alembic can commit the migration.

    ``context.configure()`` treats a connection that is already in a
    transaction as externally managed and then skips its own commit. The
    connection close rolls the migration back, and the following revision
    check reports that the database is not at head.
    """

    in_transaction = getattr(connection, "in_transaction", None)
    if not callable(in_transaction) or not in_transaction():
        return
    connection.commit()  # type: ignore[attr-defined]


def redact_connection_secrets(message: str) -> str:
    """Remove database URLs and connection credentials from an error string."""

    redacted = _DATABASE_URL.sub("[redacted-database-url]", message)
    redacted = _CONNINFO_SECRET.sub(r"\1=[redacted]", redacted)
    return _SERVER_AT.sub('server at "[redacted]"', redacted)


def raise_persistence_failure(message: str, exc: BaseException) -> None:
    """Raise a static persistence error without chaining a raw driver error."""

    raise PersistenceError(message) from PersistenceError(
        redact_connection_secrets(str(exc))
    )


def raise_redacted_migration_failure(exc: BaseException) -> None:
    """Hide connection details when a migration cannot open its database."""

    raise RuntimeError("migration connection failed") from PersistenceError(
        redact_connection_secrets(str(exc))
    )


def _install_isolated_schema(engine: Engine, schema: str) -> None:
    validated = validate_isolated_test_schema(schema)
    quoted = quoted_isolated_schema(validated)

    def _current_schema(dbapi_connection: object) -> object:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("SELECT current_schema()")
        row = cursor.fetchone()
        cursor.close()
        return row[0] if row else None

    def _pin(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute(f"SET search_path TO {quoted}")
        cursor.close()
        dbapi_connection.commit()  # type: ignore[attr-defined]
        if not isolated_schema_is_current(
            _current_schema(dbapi_connection), validated
        ):
            raise DatabaseConfigurationError(
                "refusing a connection that is not using the isolated test schema"
            )

    def _require_isolated_schema(
        dbapi_connection: object, _: object, __: object
    ) -> None:
        if not isolated_schema_is_current(
            _current_schema(dbapi_connection), validated
        ):
            raise DatabaseConfigurationError(
                "refusing a connection that is not using the isolated test schema"
            )

    engine._commitmentops_isolated_schema = validated  # type: ignore[attr-defined]
    event.listen(engine, "connect", _pin)
    event.listen(engine, "checkout", _require_isolated_schema)


def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
