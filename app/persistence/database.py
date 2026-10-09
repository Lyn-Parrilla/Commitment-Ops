"""SQLAlchemy engine and session construction.

Callers supply the database URL explicitly. Application startup does not connect
to a database, and tests can use isolated SQLite URLs without consulting
environment configuration.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    """Base for database-specific persistence records."""


SessionFactory = sessionmaker[Session]


def create_database_engine(database_url: str) -> Engine:
    """Create a PostgreSQL- or SQLite-compatible SQLAlchemy engine."""

    engine = create_engine(normalize_database_url(database_url))
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def create_session_factory(engine: Engine) -> SessionFactory:
    """Create sessions with domain-friendly post-commit object behavior."""

    return sessionmaker(bind=engine, expire_on_commit=False)


def configured_database_url(settings: Settings | None = None) -> str:
    """Read the required URL without exposing it in configuration reprs."""

    configured = (settings or get_settings()).database_url
    if configured is None or not configured.get_secret_value().strip():
        raise RuntimeError("DATABASE_URL is required for database operations")
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


def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
