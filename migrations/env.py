"""Alembic environment.

An explicit ``sqlalchemy.url`` already present on the config is used as given.
``DATABASE_URL`` is consulted only when that value is empty. A
``commitmentops_test_schema`` config attribute, when present, pins the
connection and Alembic version table to that isolated schema.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.persistence import orm  # noqa: F401  # register metadata
from app.persistence.database import (
    Base,
    configured_database_url,
    normalize_database_url,
    isolated_schema_is_current,
    quoted_isolated_schema,
    raise_redacted_migration_failure,
    release_inspection_transaction,
    resolve_migration_url,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _prepare_migration_target() -> str | None:
    explicit_url = resolve_migration_url(config.get_main_option("sqlalchemy.url"))
    if explicit_url is None:
        config.set_main_option(
            "sqlalchemy.url",
            normalize_database_url(configured_database_url()).replace("%", "%%"),
        )
    schema = config.attributes.get("commitmentops_test_schema")
    if schema is None:
        return None
    return quoted_isolated_schema(str(schema))


def run_migrations_offline() -> None:
    quoted_schema = _prepare_migration_target()
    configure_kwargs: dict[str, object] = {}
    if quoted_schema is not None:
        configure_kwargs["version_table_schema"] = quoted_schema.strip('"')
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **configure_kwargs,
    )
    with context.begin_transaction():
        context.run_migrations()


def _include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Ignore the version table when it is the connection's default schema.

    ``search_path`` makes the isolated schema the default, so reflection
    reports ``alembic_version`` with ``schema=None``. Alembic only skips that
    table when the reflected schema equals ``version_table_schema``. Those
    names differ, and ``check`` would otherwise try to drop the version table.
    """

    return not (type_ == "table" and name == "alembic_version")


def _require_isolated_search_path(connection: object, quoted_schema: str) -> str:
    """Pin and verify the isolated schema, then close the inspection transaction."""

    expected_schema = quoted_schema.strip('"')
    connection.exec_driver_sql(f"SET search_path TO {quoted_schema}")  # type: ignore[attr-defined]
    current_schema = connection.exec_driver_sql(  # type: ignore[attr-defined]
        "SELECT current_schema()"
    ).scalar()
    if not isolated_schema_is_current(current_schema, expected_schema):
        raise RuntimeError(
            "refusing to migrate because the connection is not using "
            "the isolated test schema"
        )
    release_inspection_transaction(connection)
    return expected_schema


def run_migrations_online() -> None:
    quoted_schema = _prepare_migration_target()
    try:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
            hide_parameters=True,
        )
        connection = connectable.connect()
    except Exception as exc:
        raise_redacted_migration_failure(exc)
    with connection:
        configure_kwargs: dict[str, object] = {}
        if quoted_schema is not None:
            if connection.dialect.name != "postgresql":
                raise RuntimeError(
                    "isolated test schemas require a PostgreSQL migration target"
                )
            expected_schema = _require_isolated_search_path(
                connection, quoted_schema
            )
            configure_kwargs["version_table_schema"] = expected_schema
            configure_kwargs["include_name"] = _include_name
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            **configure_kwargs,
        )
        with context.begin_transaction():
            if quoted_schema is not None:
                connection.exec_driver_sql(
                    f"SET LOCAL search_path TO {quoted_schema}"
                )
                current_schema = connection.exec_driver_sql(
                    "SELECT current_schema()"
                ).scalar()
                if not isolated_schema_is_current(
                    current_schema, quoted_schema.strip('"')
                ):
                    raise RuntimeError(
                        "refusing to migrate because the connection is not "
                        "using the isolated test schema"
                    )
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
