import traceback
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateTable

from app.config import get_settings
from app.main import create_app
from app.persistence.database import (
    create_database_engine,
    isolated_schema_is_current,
    quoted_isolated_schema,
    raise_redacted_migration_failure,
    redact_connection_secrets,
    release_inspection_transaction,
    resolve_migration_url,
    validate_isolated_test_schema,
)
from app.persistence.exceptions import DatabaseConfigurationError, PersistenceError
from app.persistence.orm import (
    AuditEventRecord,
    EvidenceRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    WorkflowRecord,
)
from app.persistence.repository import WorkflowRepository
from tests.persistence.postgres_support import (
    TEST_DATABASE_URL_ENV,
    IsolatedPostgresTarget,
    UnsafeTestDatabase,
    _dotenv_test_database_url,
    alembic_config,
    assert_postgres_test_url,
    clear_isolated_tables,
    bind_test_target,
    drop_isolated_schema,
    explicit_test_database_url,
    isolated_schema_statement,
    pin_test_search_path,
    qualified_delete_statement,
)


def test_application_database_url_is_not_the_integration_target(monkeypatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:secret-token@db.example/postgres",
    )
    monkeypatch.delenv(TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.setattr(
        "tests.persistence.postgres_support._dotenv_test_database_url",
        lambda path=None: None,
    )

    assert explicit_test_database_url() is None
    assert resolve_migration_url("") is None
    assert resolve_migration_url("   ") is None


def test_explicit_test_url_prefers_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        TEST_DATABASE_URL_ENV,
        "postgresql://from-env.example/isolated",
    )
    monkeypatch.setattr(
        "tests.persistence.postgres_support._dotenv_test_database_url",
        lambda path=None: "postgresql://from-dotenv.example/ignored",
    )

    assert explicit_test_database_url() == "postgresql://from-env.example/isolated"


def test_dotenv_reader_uses_only_the_test_database_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "# DATABASE_URL=postgresql://user:secret-token@db.example/postgres",
                "DATABASE_URL=postgresql://user:secret-token@db.example/postgres",
                "export TEST_DATABASE_URL='postgresql://db.example/isolated'",
            )
        )
    )

    assert (
        _dotenv_test_database_url(env_file) == "postgresql://db.example/isolated"
    )
    assert _dotenv_test_database_url(tmp_path / "missing.env") is None


def test_word_test_does_not_make_a_database_url_isolated() -> None:
    production = assert_postgres_test_url("postgresql://localhost/production")
    named_test = assert_postgres_test_url("postgres://localhost/test")

    assert production.endswith("/production")
    assert named_test.startswith("postgresql+psycopg://")
    assert named_test.endswith("/test")
    for schema in ("public", "auth", "test", "commitmentops_test_public"):
        with pytest.raises(DatabaseConfigurationError):
            validate_isolated_test_schema(schema)
        with pytest.raises(DatabaseConfigurationError):
            isolated_schema_statement(schema, "drop")


def test_isolated_schema_statements_name_only_the_generated_schema() -> None:
    schema = "commitmentops_test_" + "0123456789abcdef"

    assert isolated_schema_statement(schema, "create") == f'CREATE SCHEMA "{schema}"'
    assert (
        isolated_schema_statement(schema, "drop")
        == f'DROP SCHEMA "{schema}" CASCADE'
    )
    assert "public" not in isolated_schema_statement(schema, "drop")
    assert quoted_isolated_schema(schema) == f'"{schema}"'
    with pytest.raises(DatabaseConfigurationError):
        isolated_schema_statement("public", "drop")
    with pytest.raises(UnsafeTestDatabase):
        assert_postgres_test_url("sqlite+pysqlite:///:memory:")
    with pytest.raises(UnsafeTestDatabase):
        assert_postgres_test_url("postgresql://localhost/template0")


def test_schema_inspection_does_not_hide_the_committed_revision(
    tmp_path: Path,
) -> None:
    """Alembic skips its commit when configure() sees an open transaction.

    The isolated-schema check reads ``current_schema()``, which autobegins a
    transaction. Leaving that transaction open makes ``upgrade`` roll back,
    and the following ``check`` reports that the database is not at head.
    """

    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'inspect.db'}")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
            assert connection.in_transaction()
            open_context = MigrationContext.configure(connection=connection)
            assert open_context._in_external_transaction is True

        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
            release_inspection_transaction(connection)
            assert not connection.in_transaction()
            released = MigrationContext.configure(connection=connection)
            assert released._in_external_transaction is False
    finally:
        engine.dispose()

    source = (Path(__file__).parents[2] / "migrations" / "env.py").read_text()
    helper = source.split("def _require_isolated_search_path", 1)[1].split(
        "def run_migrations_online", 1
    )[0]
    online = source.split("def run_migrations_online", 1)[1]
    assert helper.index("SELECT current_schema()") < helper.index(
        "release_inspection_transaction(connection)"
    )
    assert (
        online.index("_require_isolated_search_path(")
        < online.index("context.configure(")
        < online.index("SET LOCAL search_path TO")
        < online.index("context.run_migrations()")
    )


def test_explicit_alembic_url_is_not_replaced_by_database_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    application = tmp_path / "application.db"
    isolated = tmp_path / "isolated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{application}")
    get_settings.cache_clear()

    def consulted(settings=None):
        raise AssertionError("application database URL was consulted")

    monkeypatch.setattr(
        "app.persistence.database.configured_database_url",
        consulted,
    )
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{isolated}")
    command.upgrade(config, "head")
    command.check(config)
    get_settings.cache_clear()

    assert isolated.exists()
    assert not application.exists()


def test_postgresql_offline_migration_renders_without_application_url(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def consulted(settings=None):
        raise AssertionError("application database URL was consulted")

    monkeypatch.setattr(
        "app.persistence.database.configured_database_url",
        consulted,
    )
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://placeholder@127.0.0.1/isolated",
    )
    command.upgrade(config, "head", sql=True)
    rendered = capsys.readouterr().out

    assert "CREATE TABLE organizations" in rendered
    assert "CREATE TABLE organization_memberships" in rendered
    assert "TIMESTAMP WITH TIME ZONE" in rendered
    assert "UUID" in rendered
    assert "DROP SCHEMA" not in rendered.upper()
    assert "ALTER ROLE" not in rendered.upper()
    assert "Refusing to use existing role commitmentops_app" in rendered


def test_postgresql_model_ddl_uses_native_types() -> None:
    workflow = str(
        CreateTable(WorkflowRecord.__table__).compile(dialect=postgresql.dialect())
    )
    evidence = str(
        CreateTable(EvidenceRecord.__table__).compile(dialect=postgresql.dialect())
    )
    membership = str(
        CreateTable(OrganizationMembershipRecord.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    organization = str(
        CreateTable(OrganizationRecord.__table__).compile(dialect=postgresql.dialect())
    )
    active_default = str(
        OrganizationMembershipRecord.__table__.c.is_active.server_default.arg.compile(
            dialect=postgresql.dialect()
        )
    )

    assert "UUID" in organization
    assert "TIMESTAMP WITH TIME ZONE" in evidence
    assert "JSON" in str(
        CreateTable(AuditEventRecord.__table__).compile(dialect=postgresql.dialect())
    )
    assert "ON DELETE RESTRICT" in workflow
    assert "ON DELETE CASCADE" in membership
    assert active_default == "true"
    assert "DROP SCHEMA" not in workflow.upper()
    sqlite_workflow = str(
        CreateTable(WorkflowRecord.__table__).compile(dialect=sqlite.dialect())
    )
    assert "workflows" in sqlite_workflow


def test_membership_server_default_is_declared_by_the_tenant_migration() -> None:
    source = Path("migrations/versions/20261014_02_tenant_isolation.py").read_text()

    assert "server_default=sa.true()" in source
    assert "Refusing to downgrade" in source
    assert "DROP SCHEMA" not in source.upper()


def test_application_and_migrations_do_not_drop_schemas() -> None:
    for root in (Path("app"), Path("migrations")):
        for path in root.rglob("*.py"):
            assert "DROP SCHEMA" not in path.read_text().upper()


def test_engine_creation_does_not_connect() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")

    assert engine.pool.status().endswith("size: 0")
    with engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")
    assert engine.pool.status().endswith("size: 1")
    engine.dispose()


def test_non_isolated_schema_is_rejected_without_a_connection() -> None:
    with pytest.raises(DatabaseConfigurationError, match="isolated test schema"):
        create_database_engine(
            "postgresql+psycopg://127.0.0.1:1/postgres",
            schema="public",
            connect_args={"connect_timeout": 1},
        )
    with pytest.raises(DatabaseConfigurationError, match="PostgreSQL"):
        create_database_engine(
            "sqlite+pysqlite:///:memory:",
            schema="commitmentops_test_" + ("ab" * 8),
        )


def test_health_does_not_open_the_database(monkeypatch) -> None:
    def consulted(*args, **kwargs):
        raise AssertionError("database configuration was read")

    monkeypatch.setattr("app.api.dependencies.configured_database_url", consulted)
    monkeypatch.setattr("app.api.dependencies.create_database_engine", consulted)
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_repository_failures_do_not_expose_connection_secrets() -> None:
    secret = "postgresql://user:secret-token@db.example/postgres password=secret-token"

    class ExplodingFactory:
        def __call__(self):
            raise SQLAlchemyError(
                f'could not connect: {secret} server at "db.example"'
            )

        def begin(self):
            raise SQLAlchemyError(secret)

    repository = WorkflowRepository(ExplodingFactory())  # type: ignore[arg-type]
    organization_id = uuid4()
    with pytest.raises(PersistenceError, match="workflow lookup failed") as caught:
        repository.get(uuid4(), organization_id=organization_id)

    rendered = "".join(traceback.format_exception(caught.value))
    assert "secret-token" not in rendered
    assert "db.example" not in rendered
    assert "[redacted-database-url]" in rendered
    assert redact_connection_secrets(secret) != secret


def test_connection_keywords_are_redacted() -> None:
    redacted = redact_connection_secrets(
        'host=db.example password=secret-token server at "db.example"'
    )

    assert "password=[redacted]" in redacted
    assert "host=[redacted]" in redacted
    assert "secret-token" not in redacted
    assert "db.example" not in redacted


def _schema_name() -> str:
    return "commitmentops_test_" + ("ab" * 8)


class _RecordingConnection:
    def __init__(
        self,
        *,
        current_schema: str,
        public_present: int | None = 1,
        cross_schema_references: int | None = 0,
        catalog_name: str | None = None,
    ) -> None:
        self.current_schema = current_schema
        self.public_present = public_present
        self.cross_schema_references = cross_schema_references
        self.catalog_name = catalog_name
        self.statements: list[str] = []
        self.committed = False

    def __enter__(self) -> "_RecordingConnection":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def scalar(self, statement: object, params: object = None) -> object:
        sql = str(statement)
        if "pg_constraint" in sql:
            return self.cross_schema_references
        if "schema_name = 'public'" in sql:
            return self.public_present
        if "current_schema" in sql:
            return self.current_schema
        if "schema_name = :schema" in sql:
            return self.catalog_name
        raise AssertionError(sql)

    def exec_driver_sql(self, statement: str) -> None:
        self.statements.append(statement)

    def execute(self, statement: object, params: object = None) -> None:
        self.statements.append(str(statement))

    def commit(self) -> None:
        self.committed = True


class _RecordingEngine:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection
        self.info: dict[str, object] = {}

    def connect(self) -> _RecordingConnection:
        return self.connection

    def begin(self) -> _RecordingConnection:
        return self.connection


def test_cleanup_refuses_a_schema_mismatch_before_drop() -> None:
    schema = _schema_name()
    connection = _RecordingConnection(current_schema="public", catalog_name=schema)
    engine = _RecordingEngine(connection)

    with pytest.raises(UnsafeTestDatabase, match="not the isolated test schema"):
        drop_isolated_schema(engine, schema)  # type: ignore[arg-type]

    assert connection.committed is False
    assert not any(statement.startswith("DROP") for statement in connection.statements)


def test_cleanup_refuses_when_public_is_missing_or_referenced() -> None:
    schema = _schema_name()
    missing_public = _RecordingConnection(
        current_schema=schema,
        public_present=None,
        catalog_name=schema,
    )
    with pytest.raises(UnsafeTestDatabase, match="public schema is not present"):
        drop_isolated_schema(
            _RecordingEngine(missing_public),  # type: ignore[arg-type]
            schema,
        )
    assert missing_public.statements == []

    referenced = _RecordingConnection(
        current_schema=schema,
        cross_schema_references=1,
        catalog_name=schema,
    )
    with pytest.raises(UnsafeTestDatabase, match="referenced by another schema"):
        drop_isolated_schema(
            _RecordingEngine(referenced),  # type: ignore[arg-type]
            schema,
        )
    assert not any(statement.startswith("DROP") for statement in referenced.statements)


def test_cleanup_drops_only_the_confirmed_schema() -> None:
    schema = _schema_name()
    connection = _RecordingConnection(current_schema=schema, catalog_name=schema)

    drop_isolated_schema(
        _RecordingEngine(connection),  # type: ignore[arg-type]
        schema,
    )

    assert connection.statements[-1] == f'DROP SCHEMA "{schema}" CASCADE'
    assert "public" not in connection.statements[-1]
    assert connection.committed is True


def test_row_cleanup_refuses_and_qualifies_the_isolated_schema() -> None:
    schema = _schema_name()
    mismatch = _RecordingConnection(current_schema="public")
    mismatch_engine = _RecordingEngine(mismatch)
    bind_test_target(
        mismatch_engine,  # type: ignore[arg-type]
        IsolatedPostgresTarget(
            "postgresql+psycopg://localhost/commitmentops",
            schema,
        ),
    )
    with pytest.raises(UnsafeTestDatabase, match="outside the isolated"):
        clear_isolated_tables(mismatch_engine)  # type: ignore[arg-type]
    assert mismatch.statements == []

    matched = _RecordingConnection(current_schema=schema)
    matched_engine = _RecordingEngine(matched)
    bind_test_target(
        matched_engine,  # type: ignore[arg-type]
        IsolatedPostgresTarget(
            "postgresql+psycopg://localhost/commitmentops",
            schema,
        ),
    )
    clear_isolated_tables(matched_engine)  # type: ignore[arg-type]
    assert matched.statements
    assert all(statement.startswith(f'DELETE FROM "{schema}".') for statement in matched.statements)
    assert all("public" not in statement for statement in matched.statements)
    with pytest.raises(UnsafeTestDatabase):
        qualified_delete_statement("public", "organizations")
    with pytest.raises(UnsafeTestDatabase):
        qualified_delete_statement(schema, "organizations;drop")


def test_pinned_migration_url_and_errors_hide_credentials() -> None:
    schema = _schema_name()
    target = IsolatedPostgresTarget(
        "postgresql+psycopg://user:secret-token@db.example/commitmentops",
        schema,
    )

    assert "secret-token" not in repr(target)
    assert "db.example" not in repr(target)
    pinned = pin_test_search_path(
        "postgresql+psycopg://localhost/commitmentops",
        schema,
    )
    assert schema in pinned
    assert "public" not in pinned
    config = alembic_config(
        IsolatedPostgresTarget("postgresql+psycopg://localhost/commitmentops", schema)
    )
    assert schema in config.get_main_option("sqlalchemy.url")
    assert config.attributes["commitmentops_test_schema"] == schema

    database_url = "postgresql://user:secret-token@db.example/postgres"
    with pytest.raises(RuntimeError, match="migration connection failed") as caught:
        raise_redacted_migration_failure(RuntimeError(database_url))
    rendered = "".join(traceback.format_exception(caught.value))
    assert "secret-token" not in rendered
    assert "db.example" not in rendered


def test_inconclusive_schema_identity_refuses_cleanup_and_checkout() -> None:
    schema = _schema_name()
    for current, catalog, cross_schema in (
        (None, schema, 0),
        (schema, None, 0),
        (schema, schema, None),
    ):
        connection = _RecordingConnection(
            current_schema=current,  # type: ignore[arg-type]
            catalog_name=catalog,
            cross_schema_references=cross_schema,
        )
        with pytest.raises(UnsafeTestDatabase):
            drop_isolated_schema(
                _RecordingEngine(connection),  # type: ignore[arg-type]
                schema,
            )
        assert not any(
            statement.startswith("DROP") for statement in connection.statements
        )

    rows = _RecordingConnection(current_schema=None)  # type: ignore[arg-type]
    engine = _RecordingEngine(rows)
    bind_test_target(
        engine,  # type: ignore[arg-type]
        IsolatedPostgresTarget(
            "postgresql+psycopg://localhost/commitmentops",
            schema,
        ),
    )
    with pytest.raises(UnsafeTestDatabase, match="outside the isolated"):
        clear_isolated_tables(engine)  # type: ignore[arg-type]
    assert rows.statements == []

    assert isolated_schema_is_current(None, schema) is False
    assert isolated_schema_is_current("public", schema) is False
    assert isolated_schema_is_current(schema, "public") is False
    assert isolated_schema_is_current(schema, schema) is True


def test_application_and_alembic_targets_use_the_created_schema() -> None:
    schema = _schema_name()
    other = "commitmentops_test_" + ("cd" * 8)
    url = "postgresql+psycopg://localhost/production"
    pinned = pin_test_search_path(url, schema)
    config = alembic_config(IsolatedPostgresTarget(url, schema))
    engine = create_database_engine(
        url, schema=schema, connect_args={"connect_timeout": 1}
    )
    rendered_target = engine.url.render_as_string(hide_password=False)
    pinned_schema = getattr(engine, "_commitmentops_isolated_schema", None)
    status = engine.pool.status()
    engine.dispose()

    assert schema in pinned
    assert other not in pinned
    assert "public" not in pinned
    assert config.attributes["commitmentops_test_schema"] == schema
    assert schema in config.get_main_option("sqlalchemy.url")
    assert other not in config.get_main_option("sqlalchemy.url")
    assert pinned_schema == schema
    assert "public" not in rendered_target
    assert other not in rendered_target
    assert "Connections in pool: 0" in status
    assert "Current Checked out connections: 0" in status


def test_public_schema_cleanup_does_not_open_a_connection() -> None:
    class ExplodingEngine:
        def connect(self) -> None:
            raise AssertionError("cleanup connected")

    with pytest.raises(DatabaseConfigurationError):
        drop_isolated_schema(ExplodingEngine(), "public")  # type: ignore[arg-type]


def test_isolated_engine_configuration_does_not_connect() -> None:
    engine = create_database_engine(
        "postgresql+psycopg://127.0.0.1:1/postgres",
        schema=_schema_name(),
        connect_args={"connect_timeout": 1},
    )
    status = engine.pool.status()
    engine.dispose()

    assert "Connections in pool: 0" in status
    assert "Current Checked out connections: 0" in status
