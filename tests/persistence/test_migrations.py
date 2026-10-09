from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.config import get_settings
from app.persistence import create_database_engine


def test_initial_migration_creates_accountability_schema(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration-test.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()

    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)

    engine = create_database_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    get_settings.cache_clear()

    assert {
        "alembic_version",
        "audit_events",
        "evidence",
        "people",
        "requirements",
        "workflows",
    } <= tables
