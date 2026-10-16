"""SQLAlchemy engine and session construction.

Callers supply the database URL explicitly. Application startup does not connect
to a database, and tests can use isolated SQLite URLs without consulting
environment configuration.
"""

from __future__ import annotations

import re

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
