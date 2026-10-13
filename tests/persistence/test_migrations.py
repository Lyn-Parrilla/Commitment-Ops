from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.config import get_settings
from app.persistence import create_database_engine


def test_docker_build_context_packages_migration_runtime_files() -> None:
    repository_root = Path(__file__).parents[2]
    dockerfile = (repository_root / "Dockerfile").read_text()
    dockerignore = [
        line.strip()
        for line in (repository_root / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert "WORKDIR /app" in dockerfile
    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert "alembic.ini" not in dockerignore
    assert "migrations" not in dockerignore
    assert not any(
        _dockerignore_excludes(pattern, "alembic.ini")
        for pattern in dockerignore
    )
    assert not any(
        _dockerignore_excludes(pattern, "migrations")
        for pattern in dockerignore
    )
    assert (repository_root / "migrations" / "env.py").is_file()
    assert (repository_root / "migrations" / "script.py.mako").is_file()
    assert any((repository_root / "migrations" / "versions").glob("*.py"))


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


def _dockerignore_excludes(pattern: str, required_path: str) -> bool:
    if pattern.startswith("!"):
        return False
    normalized = pattern.lstrip("/").rstrip("/")
    if normalized.endswith("/**"):
        normalized = normalized.removesuffix("/**").rstrip("/")
    return normalized in {required_path, f"**/{required_path}"}
