"""Transaction-local tenant context for PostgreSQL row-level security.

SQLite has no policies. On PostgreSQL every application transaction switches
to the non-bypass application role and sets the verified user and organization
for that transaction only. Both settings clear on commit or rollback.
Organization and membership bootstrap uses a separate privileged connection.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.persistence.database import SessionFactory
from app.persistence.exceptions import PersistenceError

APP_ROLE = "commitmentops_app"
RUNTIME_LOGIN_REFUSAL = (
    "Refusing the runtime database login because it is a superuser, "
    "has BYPASSRLS, or can assume a privileged role"
)


def runtime_login_block_reason(bind: object, role_name: str | None = None) -> str | None:
    """Return a refusal when this login can bypass tenant policies.

    The login itself and every role it can assume through direct or indirect
    membership are checked. Nothing is altered or revoked.
    """

    if _dialect_name(bind) != "postgresql":
        return None
    row = bind.execute(  # type: ignore[attr-defined]
        text(
            """
            WITH RECURSIVE assumed(oid) AS (
                SELECT oid
                FROM pg_roles
                WHERE rolname = COALESCE(CAST(:role AS text), session_user::text)
                UNION
                SELECT membership.roleid
                FROM pg_auth_members AS membership
                JOIN assumed ON assumed.oid = membership.member
            )
            SELECT
                login.rolcanlogin,
                EXISTS (
                    SELECT 1
                    FROM assumed
                    JOIN pg_roles AS granted ON granted.oid = assumed.oid
                    WHERE granted.rolsuper
                       OR granted.rolbypassrls
                       OR granted.rolcreaterole
                       OR granted.rolcreatedb
                ) AS assumes_privileged,
                EXISTS (
                    SELECT 1
                    FROM assumed
                    JOIN pg_namespace
                      ON pg_namespace.nspowner = assumed.oid
                    WHERE pg_namespace.nspname = current_schema()
                )
                OR EXISTS (
                    SELECT 1
                    FROM assumed
                    JOIN pg_class ON pg_class.relowner = assumed.oid
                    JOIN pg_namespace
                      ON pg_namespace.oid = pg_class.relnamespace
                    WHERE pg_namespace.nspname = current_schema()
                      AND pg_class.relname IN (
                          'organizations',
                          'organization_memberships',
                          'people',
                          'workflows',
                          'requirements',
                          'evidence',
                          'audit_events'
                      )
                ) AS assumes_owner
            FROM pg_roles AS login
            WHERE login.rolname = COALESCE(
                CAST(:role AS text),
                session_user::text
            )
            """
        ),
        {"role": role_name},
    ).one_or_none()
    if (
        row is None
        or not row.rolcanlogin
        or row.assumes_privileged
        or row.assumes_owner
    ):
        return RUNTIME_LOGIN_REFUSAL
    return None


def apply_tenant_context(
    session: Session,
    *,
    user_id: UUID | None,
    organization_id: UUID | None,
) -> None:
    """Fail closed unless this transaction is subject to tenant policies."""

    if session.get_bind().dialect.name != "postgresql":
        return
    reason = runtime_login_block_reason(session)
    if reason is not None:
        raise PersistenceError(reason)
    session.execute(text("SET LOCAL ROLE commitmentops_app"))
    session.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": "" if user_id is None else str(user_id)},
    )
    session.execute(
        text("SELECT set_config('app.organization_id', :organization_id, true)"),
        {
            "organization_id": (
                "" if organization_id is None else str(organization_id)
            )
        },
    )
    (
        role_name,
        is_superuser,
        bypasses_rls,
        can_login,
        can_create_db,
        can_create_role,
        can_assume,
    ) = session.execute(
        text(
            """
            SELECT
                rolname,
                rolsuper,
                rolbypassrls,
                rolcanlogin,
                rolcreatedb,
                rolcreaterole,
                pg_has_role(session_user, :role, 'MEMBER')
            FROM pg_roles
            WHERE rolname = current_user
            """
        ),
        {"role": APP_ROLE},
    ).one()
    if (
        role_name != APP_ROLE
        or is_superuser
        or bypasses_rls
        or can_login
        or can_create_db
        or can_create_role
        or not can_assume
    ):
        raise PersistenceError("tenant enforcement could not be established")


def _dialect_name(bind: object) -> str:
    dialect = getattr(bind, "dialect", None)
    if dialect is None:
        dialect = bind.get_bind().dialect  # type: ignore[attr-defined]
    return dialect.name


@contextmanager
def tenant_session(
    session_factory: SessionFactory,
    *,
    write: bool,
    user_id: UUID | None = None,
    organization_id: UUID | None = None,
) -> Iterator[Session]:
    """Open one transaction and install tenant context before any query."""

    opener = session_factory.begin if write else session_factory
    with opener() as session:
        apply_tenant_context(
            session,
            user_id=user_id,
            organization_id=organization_id,
        )
        yield session


@contextmanager
def privileged_session(session_factory: SessionFactory) -> Iterator[Session]:
    """Open a bootstrap transaction that does not switch to the application role.

    PostgreSQL allows this only for a superuser or a login with ``BYPASSRLS``.
    The runtime login is neither, so request sessions cannot use this path to
    skip tenant policies.
    """

    with session_factory.begin() as session:
        if session.get_bind().dialect.name == "postgresql":
            is_superuser, bypasses_rls = session.execute(
                text(
                    """
                    SELECT rolsuper, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = current_user
                    """
                )
            ).one()
            if not is_superuser and not bypasses_rls:
                raise PersistenceError(
                    "organization administration is unavailable"
                )
        yield session
