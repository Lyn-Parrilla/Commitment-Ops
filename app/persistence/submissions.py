"""Persistence for requirement submission links and public evidence appends.

PostgreSQL public writes go through a security-definer function. That
function is the only path that can insert evidence without a membership.
SQLite tests apply the same acceptance rules in this repository.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.enums import EvidenceStatus, EvidenceType, RequirementStatus, WorkflowStatus
from app.domain.evidence_files import (
    InspectedEvidenceFile,
    build_storage_key,
    require_storage_key,
)
from app.domain.exceptions import DomainValidationError
from app.domain.models import Evidence
from app.domain.submissions import (
    normalize_submission_token,
    redact_submission_token,
    submission_actor_reference,
    submission_token_hash,
    validate_https_reference,
    validate_submission_description,
    validate_submitter_name,
)
from app.persistence.database import (
    SessionFactory,
    raise_persistence_failure,
    redact_connection_secrets,
)
from app.persistence.exceptions import PersistenceError
from app.persistence.object_storage import ObjectStorage
from app.persistence.orm import (
    AuditEventRecord,
    EvidenceFileRecord,
    EvidenceFileReservationRecord,
    EvidenceRecord,
    EvidenceStorageCleanupRecord,
    OrganizationRecord,
    RequirementRecord,
    SubmissionLinkRecord,
    WorkflowRecord,
)
from app.persistence.rls import tenant_session

_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT_FUNCTION = "public_submission_context"
_SUBMIT_FUNCTION = "submit_public_evidence"
_TARGET_FUNCTION = "submission_target_organization"
_RESERVE_FUNCTION = "reserve_evidence_object"
_SUBMIT_FILE_FUNCTION = "submit_public_evidence_with_file"
_RECORD_FUNCTION = "record_evidence_storage_failure"
_RELEASE_FUNCTION = "release_evidence_object_reservation"
_CLAIM_FUNCTION = "claim_evidence_storage_key"
_COMPLETE_FUNCTION = "complete_evidence_storage_cleanup"
_CONFIRM_FUNCTION = "confirm_evidence_storage_claim"
_FILE_FUNCTIONS = {
    _TARGET_FUNCTION,
    _RESERVE_FUNCTION,
    _SUBMIT_FILE_FUNCTION,
    _RECORD_FUNCTION,
    _RELEASE_FUNCTION,
    _CLAIM_FUNCTION,
    _COMPLETE_FUNCTION,
    _CONFIRM_FUNCTION,
}
CLEANUP_GRACE_SECONDS = 86400
CLAIM_RETRY_SECONDS = 300
_MAINTENANCE_ROLE = "commitmentops_maintenance"
_ADVISORY_LOCK = (
    "SELECT pg_catalog.pg_advisory_lock("
    "pg_catalog.hashtextextended(CAST(:storage_key AS text), 0::bigint))"
)
_ADVISORY_UNLOCK = (
    "SELECT pg_catalog.pg_advisory_unlock("
    "pg_catalog.hashtextextended(CAST(:storage_key AS text), 0::bigint))"
)


@dataclass(frozen=True)
class StoredSubmissionLink:
    id: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    max_uses: int
    use_count: int
    last_used_at: datetime | None


@dataclass(frozen=True)
class PublicSubmissionContext:
    organization_name: str
    requirement_title: str
    evidence_expectation: str


class SubmissionRepository:
    """Store link hashes and append public evidence for one requirement."""

    def __init__(
        self,
        session_factory: SessionFactory,
        storage: ObjectStorage | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage

    def create_link(
        self,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        requirement_id: UUID,
        token_hash: str,
        created_by_user_id: UUID,
        expires_at: datetime,
        max_uses: int,
        user_id: UUID,
    ) -> StoredSubmissionLink:
        if _HASH.fullmatch(token_hash) is None:
            raise PersistenceError("submission token hash is invalid")
        if max_uses < 1:
            raise PersistenceError("submission link use limit is invalid")
        table = SubmissionLinkRecord.__table__
        link_id = uuid4()
        now = datetime.now(UTC)
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                session.execute(
                    insert(table).values(
                        id=link_id,
                        organization_id=organization_id,
                        workflow_id=workflow_id,
                        requirement_id=requirement_id,
                        token_hash=token_hash,
                        created_at=now,
                        expires_at=expires_at,
                        revoked_at=None,
                        created_by_user_id=created_by_user_id,
                        max_uses=max_uses,
                        use_count=0,
                        last_used_at=None,
                    )
                )
                row = session.execute(
                    select(*_public_link_columns()).where(table.c.id == link_id)
                ).one()
                return _link_from_row(row)
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            _raise_submission_failure(exc)

    def list_links(
        self,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        requirement_id: UUID,
        user_id: UUID,
    ) -> list[StoredSubmissionLink]:
        table = SubmissionLinkRecord.__table__
        statement = (
            select(*_public_link_columns())
            .where(
                table.c.organization_id == organization_id,
                table.c.workflow_id == workflow_id,
                table.c.requirement_id == requirement_id,
            )
            .order_by(table.c.created_at, table.c.id)
        )
        try:
            with tenant_session(
                self._session_factory,
                write=False,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                return [
                    _link_from_row(row) for row in session.execute(statement).all()
                ]
        except SQLAlchemyError as exc:
            _raise_submission_failure(exc)

    def revoke_link(
        self,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        requirement_id: UUID,
        link_id: UUID,
        user_id: UUID,
    ) -> StoredSubmissionLink | None:
        table = SubmissionLinkRecord.__table__
        scope = (
            table.c.id == link_id,
            table.c.organization_id == organization_id,
            table.c.workflow_id == workflow_id,
            table.c.requirement_id == requirement_id,
        )
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                session.execute(
                    update(table)
                    .where(*scope, table.c.revoked_at.is_(None))
                    .values(revoked_at=datetime.now(UTC))
                )
                row = session.execute(
                    select(*_public_link_columns()).where(*scope)
                ).one_or_none()
                return _link_from_row(row) if row else None
        except SQLAlchemyError as exc:
            _raise_submission_failure(exc)

    def public_context(self, raw_token: str | None) -> PublicSubmissionContext | None:
        token = normalize_submission_token(raw_token)
        if token is None:
            return None
        try:
            with tenant_session(
                self._session_factory,
                write=False,
                user_id=None,
                organization_id=None,
            ) as session:
                if _is_postgres(session):
                    return _postgres_context(session, token)
                return _sqlite_context(
                    session,
                    submission_token_hash(token),
                    datetime.now(UTC),
                )
        except SQLAlchemyError as exc:
            _raise_submission_failure(exc, token)

    def public_submit(
        self,
        raw_token: str | None,
        *,
        description: str,
        reference: str,
        submitter_name: str | None,
    ) -> UUID | None:
        """Append one evidence row, or return None when the link cannot be used."""

        cleaned_description = validate_submission_description(description)
        cleaned_reference = validate_https_reference(reference)
        cleaned_name = validate_submitter_name(submitter_name)
        token = normalize_submission_token(raw_token)
        if token is None:
            return None
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=None,
                organization_id=None,
            ) as session:
                if _is_postgres(session):
                    return _postgres_submit(
                        session,
                        token,
                        description=cleaned_description,
                        reference=cleaned_reference,
                        submitter_name=cleaned_name,
                    )
                return _sqlite_submit(
                    session,
                    submission_token_hash(token),
                    description=cleaned_description,
                    reference=cleaned_reference,
                    submitter_name=cleaned_name,
                    submitted_at=datetime.now(UTC),
                )
        except DomainValidationError:
            raise
        except SQLAlchemyError as exc:
            if _invalid_submission(exc):
                raise DomainValidationError("The submission is not valid.") from None
            _raise_submission_failure(exc, token)

    def public_submit_file(
        self,
        raw_token: str | None,
        *,
        description: str,
        reference: str | None,
        submitter_name: str | None,
        inspected: InspectedEvidenceFile,
    ) -> UUID | None:
        """Store one private file and append evidence, or return None."""

        cleaned_description = validate_submission_description(description)
        cleaned_reference = (
            None if reference is None else validate_https_reference(reference)
        )
        cleaned_name = validate_submitter_name(submitter_name)
        if self._storage is None:
            raise PersistenceError("evidence file storage is not configured")
        token = normalize_submission_token(raw_token)
        if token is None:
            return None
        evidence_id = uuid4()
        organization_id = self._target_organization(token)
        if organization_id is None:
            return None
        key = build_storage_key(
            str(organization_id),
            str(evidence_id),
            secrets.token_hex(16),
        )
        if self._reserve(token, evidence_id, key) is None:
            return None
        try:
            self._storage.put_new(key, inspected.content, inspected.media_type)
            stored_id = self._commit_file(
                token,
                evidence_id=evidence_id,
                storage_key=key,
                description=cleaned_description,
                reference=cleaned_reference,
                submitter_name=cleaned_name,
                inspected=inspected,
            )
        except Exception:
            self._discard_object(token, key)
            raise
        if stored_id is None:
            self._discard_object(token, key)
        return stored_id

    def file_download_status(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        workflow_id: UUID,
        requirement_id: UUID,
        evidence_id: UUID,
    ) -> str:
        """Return missing or unscanned. This phase never releases file bytes."""

        with tenant_session(
            self._session_factory,
            write=False,
            user_id=user_id,
            organization_id=organization_id,
        ) as session:
            row = session.execute(
                select(EvidenceFileRecord.scan_status)
                .join(
                    EvidenceRecord,
                    EvidenceRecord.id == EvidenceFileRecord.evidence_id,
                )
                .join(
                    RequirementRecord,
                    RequirementRecord.id == EvidenceRecord.requirement_id,
                )
                .join(
                    WorkflowRecord,
                    WorkflowRecord.id == RequirementRecord.workflow_id,
                )
                .where(
                    EvidenceFileRecord.evidence_id == evidence_id,
                    EvidenceFileRecord.organization_id == organization_id,
                    EvidenceRecord.requirement_id == requirement_id,
                    RequirementRecord.workflow_id == workflow_id,
                    WorkflowRecord.organization_id == organization_id,
                )
            ).one_or_none()
        if row is None:
            return "missing"
        return "unscanned"

    def cleanup_unreferenced_objects(
        self,
        *,
        grace_seconds: int = CLEANUP_GRACE_SECONDS,
    ) -> int:
        """Claim an eligible key, then delete that object.

        The database claim commits before any storage delete. PostgreSQL then
        holds a session-level advisory lock on
        ``hashtextextended(storage_key, 0)``, the same key used by
        ``claim_evidence_storage_key``. That lock stays held across the
        authorization recheck, the storage delete, and completion, and it is
        released on every return path. The claim transaction is not left
        open during the storage request. A second worker's claim takes a
        transaction lock on that same key, so it cannot rotate the token
        while this lock is held. The recheck requires the original claim
        token, no evidence-file row, and no active reservation. A failed
        recheck or a failed storage delete leaves the object in place. If
        the storage delete succeeds and completing the cleanup row fails,
        the object is already gone, the row remains, the lock is released,
        and that key is not counted. Deleting a missing key succeeds, so a
        later pass can finish the row.

        SQLite has no session advisory lock. Its write transaction starts
        after the storage existence check and stays open across the recheck,
        delete, and completion. A second connection cannot change the claim
        row during that transaction.

        This process does not start a cleanup worker. The application login
        can assume ``commitmentops_app`` only. ``_maintenance_session`` sets
        ``commitmentops_maintenance`` and fails closed, before any delete,
        when that role cannot be assumed. Do not grant the maintenance role
        to the application login.
        """

        if grace_seconds < CLEANUP_GRACE_SECONDS:
            raise PersistenceError("evidence cleanup grace period is too short")
        if self._storage is None:
            raise PersistenceError("evidence file storage is not configured")
        deleted = 0
        while True:
            claimed = self._claim_one(grace_seconds)
            if claimed is None:
                return deleted
            key, token = claimed
            try:
                require_storage_key(key)
            except DomainValidationError as exc:
                raise PersistenceError(
                    "evidence cleanup refused an invalid storage key"
                ) from exc
            if self._delete_claimed_object(key, token):
                deleted += 1

    def _target_organization(self, token: str) -> UUID | None:
        try:
            with tenant_session(
                self._session_factory,
                write=False,
                user_id=None,
                organization_id=None,
            ) as session:
                if _is_postgres(session):
                    value = session.execute(
                        text(
                            f"SELECT {_qualified_function(session, _TARGET_FUNCTION)}(:token)"
                        ),
                        {"token": token},
                    ).scalar_one_or_none()
                    return None if value is None else UUID(str(value))
                return _sqlite_target_organization(
                    session, submission_token_hash(token), datetime.now(UTC)
                )
        except SQLAlchemyError as exc:
            _raise_submission_failure(exc, token)

    def _reserve(self, token: str, evidence_id: UUID, key: str) -> UUID | None:
        require_storage_key(key)
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=None,
                organization_id=None,
            ) as session:
                if _is_postgres(session):
                    value = session.execute(
                        text(
                            "SELECT "
                            f"{_qualified_function(session, _RESERVE_FUNCTION)}"
                            "(:token, :evidence_id, :storage_key)"
                        ),
                        {
                            "token": token,
                            "evidence_id": evidence_id,
                            "storage_key": key,
                        },
                    ).scalar_one_or_none()
                    return None if value is None else UUID(str(value))
                return _sqlite_reserve(
                    session,
                    submission_token_hash(token),
                    evidence_id,
                    key,
                    datetime.now(UTC),
                )
        except SQLAlchemyError as exc:
            if _invalid_submission(exc):
                raise DomainValidationError("The submission is not valid.") from None
            _raise_submission_failure(exc, token)

    def _commit_file(
        self,
        token: str,
        *,
        evidence_id: UUID,
        storage_key: str,
        description: str,
        reference: str | None,
        submitter_name: str | None,
        inspected: InspectedEvidenceFile,
    ) -> UUID | None:
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=None,
                organization_id=None,
            ) as session:
                if _is_postgres(session):
                    value = session.execute(
                        text(
                            "SELECT "
                            f"{_qualified_function(session, _SUBMIT_FILE_FUNCTION)}"
                            "(:token, :description, :reference, :submitter_name, "
                            ":evidence_id, :storage_key, :filename, :media_type, "
                            ":byte_size, :checksum)"
                        ),
                        {
                            "token": token,
                            "description": description,
                            "reference": reference,
                            "submitter_name": submitter_name,
                            "evidence_id": evidence_id,
                            "storage_key": storage_key,
                            "filename": inspected.filename,
                            "media_type": inspected.media_type,
                            "byte_size": inspected.byte_size,
                            "checksum": inspected.checksum_sha256,
                        },
                    ).scalar_one_or_none()
                    return None if value is None else UUID(str(value))
                return _sqlite_submit_file(
                    session,
                    submission_token_hash(token),
                    evidence_id=evidence_id,
                    storage_key=storage_key,
                    description=description,
                    reference=reference,
                    submitter_name=submitter_name,
                    inspected=inspected,
                    submitted_at=datetime.now(UTC),
                )
        except SQLAlchemyError as exc:
            if _invalid_submission(exc):
                raise DomainValidationError("The submission is not valid.") from None
            _raise_submission_failure(exc, token)

    def _discard_object(self, token: str, key: str) -> None:
        storage = self._storage
        if storage is None:
            raise PersistenceError("evidence file storage is not configured")
        try:
            storage.delete(key)
        except Exception:
            self._record_cleanup(token, key)
            return
        self._release_reservation(token, key)

    def _record_cleanup(self, token: str, key: str) -> None:
        require_storage_key(key)
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=None,
                organization_id=None,
            ) as session:
                if _is_postgres(session):
                    session.execute(
                        text(
                            "SELECT "
                            f"{_qualified_function(session, _RECORD_FUNCTION)}"
                            "(:token, :storage_key)"
                        ),
                        {"token": token, "storage_key": key},
                    )
                    return
                reservation = _sqlite_owned_reservation(session, token, key)
                if _sqlite_key_referenced(session, key):
                    raise PersistenceError(
                        "evidence cleanup refused a referenced object"
                    )
                session.add(
                    EvidenceStorageCleanupRecord(
                        storage_key=key,
                        organization_id=reservation.organization_id,
                        recorded_at=datetime.now(UTC),
                    )
                )
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError(
                "evidence object cleanup could not be recorded"
            ) from exc

    def _release_reservation(self, token: str, key: str) -> None:
        require_storage_key(key)
        with tenant_session(
            self._session_factory,
            write=True,
            user_id=None,
            organization_id=None,
        ) as session:
            if _is_postgres(session):
                session.execute(
                    text(
                        "SELECT "
                        f"{_qualified_function(session, _RELEASE_FUNCTION)}"
                        "(:token, :storage_key)"
                    ),
                    {"token": token, "storage_key": key},
                )
                return
            reservation = _sqlite_owned_reservation(session, token, key)
            if _sqlite_key_referenced(session, key):
                raise PersistenceError("evidence cleanup refused a referenced object")
            session.delete(reservation)

    def _delete_claimed_object(self, key: str, token: UUID) -> bool:
        storage = self._storage
        if storage is None:
            raise PersistenceError("evidence file storage is not configured")
        session = self._session_factory()
        try:
            if _is_postgres(session):
                bind = session.get_bind()
                session.close()
                return self._postgres_delete_claimed_object(bind, storage, key, token)
            _require_storage_object(storage, key)
            return self._sqlite_delete_claimed_object(session, storage, key, token)
        finally:
            session.close()

    def _postgres_delete_claimed_object(self, bind, storage, key: str, token: UUID) -> bool:
        """Hold one connection so the session advisory lock survives commit."""

        connection = bind.connect()
        locked = False
        try:
            self._postgres_lock(connection, key)
            locked = True
            _require_storage_object(storage, key)
            if not self._postgres_confirm(connection, key, token):
                return False
            _delete_storage_object(storage, key)
            self._postgres_complete(connection, key, token)
            return True
        finally:
            if locked:
                self._postgres_unlock(connection, key)
            connection.close()

    def _postgres_lock(self, connection, key: str) -> None:
        try:
            with connection.begin():
                self._assume_maintenance_connection(connection)
                connection.execute(text(_ADVISORY_LOCK), {"storage_key": key})
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(
                "evidence cleanup could not verify the database"
            ) from exc

    def _postgres_confirm(self, connection, key: str, token: UUID) -> bool:
        try:
            with connection.begin():
                self._assume_maintenance_connection(connection)
                allowed = connection.scalar(
                    text(
                        "SELECT "
                        f"{_qualified_function_connection(connection, _CONFIRM_FUNCTION)}"
                        "(:storage_key, :claim_token)"
                    ),
                    {"storage_key": key, "claim_token": token},
                )
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(
                "evidence cleanup could not verify the database"
            ) from exc
        return bool(allowed)

    def _postgres_complete(self, connection, key: str, token: UUID) -> None:
        require_storage_key(key)
        try:
            with connection.begin():
                self._assume_maintenance_connection(connection)
                connection.execute(
                    text(
                        "SELECT "
                        f"{_qualified_function_connection(connection, _COMPLETE_FUNCTION)}"
                        "(:storage_key, :claim_token)"
                    ),
                    {"storage_key": key, "claim_token": token},
                )
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(
                "evidence cleanup could not verify the database"
            ) from exc

    def _postgres_unlock(self, connection, key: str) -> None:
        try:
            if connection.in_transaction():
                connection.rollback()
            connection.execute(text(_ADVISORY_UNLOCK), {"storage_key": key})
            connection.commit()
        except Exception:
            connection.invalidate()

    def _sqlite_delete_claimed_object(
        self,
        session: Session,
        storage: ObjectStorage,
        key: str,
        token: UUID,
    ) -> bool:
        transaction = session.begin()
        try:
            session.execute(
                text(
                    """
                    UPDATE evidence_storage_cleanup
                       SET claimed_at = claimed_at
                     WHERE storage_key = :storage_key
                    """
                ),
                {"storage_key": key},
            )
            if not _sqlite_claim_authorized(session, key, token):
                transaction.rollback()
                return False
            _delete_storage_object(storage, key)
            _sqlite_complete_claim(session, key, token)
            transaction.commit()
        except Exception:
            if transaction.is_active:
                transaction.rollback()
            raise
        return True

    def _assume_maintenance(self, session: Session) -> None:
        if not _is_postgres(session):
            return
        self._assume_maintenance_connection(session)

    def _assume_maintenance_connection(self, connection) -> None:
        connection.execute(text(f"SET LOCAL ROLE {_MAINTENANCE_ROLE}"))
        role = connection.scalar(text("SELECT current_user"))
        if role != _MAINTENANCE_ROLE:
            raise PersistenceError("evidence cleanup could not be authorized")

    def _claim_one(self, grace_seconds: int) -> tuple[str, UUID] | None:
        try:
            with self._maintenance_session() as session:
                if _is_postgres(session):
                    row = session.execute(
                        text(
                            "SELECT claimed_storage_key, claimed_token FROM "
                            f"{_qualified_function(session, _CLAIM_FUNCTION)}(:grace)"
                        ),
                        {"grace": grace_seconds},
                    ).one_or_none()
                    if row is None:
                        return None
                    return str(row.claimed_storage_key), UUID(str(row.claimed_token))
                return _sqlite_claim(session, grace_seconds)
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(
                "evidence cleanup could not verify the database"
            ) from exc

    def _complete_claim(self, key: str, token: UUID) -> None:
        require_storage_key(key)
        try:
            with self._maintenance_session() as session:
                if _is_postgres(session):
                    session.execute(
                        text(
                            "SELECT "
                            f"{_qualified_function(session, _COMPLETE_FUNCTION)}"
                            "(:storage_key, :claim_token)"
                        ),
                        {"storage_key": key, "claim_token": token},
                    )
                    return
                _sqlite_complete_claim(session, key, token)
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(
                "evidence cleanup could not verify the database"
            ) from exc

    @contextmanager
    def _maintenance_session(self) -> Iterator[Session]:
        with self._session_factory.begin() as session:
            self._assume_maintenance(session)
            yield session


def _public_link_columns():
    """Columns the application role may read. The hash is not among them."""

    table = SubmissionLinkRecord.__table__
    return (
        table.c.id,
        table.c.created_at,
        table.c.expires_at,
        table.c.revoked_at,
        table.c.max_uses,
        table.c.use_count,
        table.c.last_used_at,
    )


def _link_from_row(row) -> StoredSubmissionLink:
    return StoredSubmissionLink(
        id=row.id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        max_uses=row.max_uses,
        use_count=row.use_count,
        last_used_at=row.last_used_at,
    )


def _is_postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def _qualified_function(session: Session, name: str) -> str:
    return _qualified_function_connection(session, name)


def _qualified_function_connection(connection, name: str) -> str:
    if name not in {_CONTEXT_FUNCTION, _SUBMIT_FUNCTION, *_FILE_FUNCTIONS}:
        raise PersistenceError("submission function is unavailable")
    schema = connection.scalar(text("SELECT current_schema()"))
    if not isinstance(schema, str) or _SCHEMA_NAME.fullmatch(schema) is None:
        raise PersistenceError("submission function schema is unavailable")
    return f'"{schema}".{name}'


def _postgres_context(
    session: Session,
    token: str,
) -> PublicSubmissionContext | None:
    function_name = _qualified_function(session, _CONTEXT_FUNCTION)
    row = session.execute(
        text(
            "SELECT organization_name, requirement_title, evidence_expectation "
            f"FROM {function_name}(:token)"
        ),
        {"token": token},
    ).one_or_none()
    if row is None:
        return None
    return PublicSubmissionContext(
        organization_name=row.organization_name,
        requirement_title=row.requirement_title,
        evidence_expectation=row.evidence_expectation,
    )


def _postgres_submit(
    session: Session,
    token: str,
    *,
    description: str,
    reference: str,
    submitter_name: str | None,
) -> UUID | None:
    function_name = _qualified_function(session, _SUBMIT_FUNCTION)
    evidence_id = session.scalar(
        text(
            f"SELECT {function_name}("
            ":token, :description, :reference, :submitter_name)"
        ),
        {
            "token": token,
            "description": description,
            "reference": reference,
            "submitter_name": submitter_name,
        },
    )
    if evidence_id is None:
        return None
    return evidence_id if isinstance(evidence_id, UUID) else UUID(str(evidence_id))


def _sqlite_context(
    session: Session,
    token_hash: str,
    now: datetime,
) -> PublicSubmissionContext | None:
    row = session.execute(
        select(
            OrganizationRecord.display_name,
            OrganizationRecord.name,
            RequirementRecord.title,
            RequirementRecord.evidence_expectation,
        )
        .select_from(SubmissionLinkRecord)
        .join(
            RequirementRecord,
            RequirementRecord.id == SubmissionLinkRecord.requirement_id,
        )
        .join(
            WorkflowRecord,
            WorkflowRecord.id == SubmissionLinkRecord.workflow_id,
        )
        .join(
            OrganizationRecord,
            OrganizationRecord.id == SubmissionLinkRecord.organization_id,
        )
        .where(
            SubmissionLinkRecord.token_hash == token_hash,
            SubmissionLinkRecord.workflow_id == RequirementRecord.workflow_id,
            WorkflowRecord.organization_id == SubmissionLinkRecord.organization_id,
            *_open_link(now),
        )
    ).one_or_none()
    if row is None:
        return None
    display_name = row.display_name.strip() if row.display_name else ""
    return PublicSubmissionContext(
        organization_name=display_name or row.name,
        requirement_title=row.title,
        evidence_expectation=row.evidence_expectation,
    )


def _sqlite_submit(
    session: Session,
    token_hash: str,
    *,
    description: str,
    reference: str,
    submitter_name: str | None,
    submitted_at: datetime,
) -> UUID | None:
    candidate = session.execute(
        select(
            SubmissionLinkRecord.id,
            SubmissionLinkRecord.organization_id,
            SubmissionLinkRecord.workflow_id,
            SubmissionLinkRecord.requirement_id,
            SubmissionLinkRecord.use_count,
        )
        .select_from(SubmissionLinkRecord)
        .join(
            RequirementRecord,
            RequirementRecord.id == SubmissionLinkRecord.requirement_id,
        )
        .join(
            WorkflowRecord,
            WorkflowRecord.id == SubmissionLinkRecord.workflow_id,
        )
        .where(
            SubmissionLinkRecord.token_hash == token_hash,
            SubmissionLinkRecord.workflow_id == RequirementRecord.workflow_id,
            WorkflowRecord.organization_id == SubmissionLinkRecord.organization_id,
            *_open_link(submitted_at),
        )
    ).one_or_none()
    if candidate is None:
        return None
    updated = session.execute(
        update(SubmissionLinkRecord)
        .where(
            SubmissionLinkRecord.id == candidate.id,
            SubmissionLinkRecord.revoked_at.is_(None),
            SubmissionLinkRecord.expires_at > submitted_at,
            SubmissionLinkRecord.use_count == candidate.use_count,
            SubmissionLinkRecord.use_count < SubmissionLinkRecord.max_uses,
        )
        .values(
            use_count=candidate.use_count + 1,
            last_used_at=submitted_at,
        )
    )
    if updated.rowcount != 1:
        return None
    evidence = Evidence(
        description=description,
        evidence_type=EvidenceType.LINK,
        submitted_by=None,
        submitted_at=submitted_at,
        reference=reference,
        status=EvidenceStatus.SUBMITTED,
        submitter_name=submitter_name,
        identity_verified=False,
    )
    session.add(
        EvidenceRecord(
            id=evidence.id,
            requirement_id=candidate.requirement_id,
            description=evidence.description,
            evidence_type=evidence.evidence_type.value,
            submitted_by_id=None,
            submitted_at=evidence.submitted_at,
            reference=evidence.reference,
            status=evidence.status.value,
            submitter_name=evidence.submitter_name,
            identity_verified=False,
        )
    )
    session.add(
        AuditEventRecord(
            occurred_at=submitted_at,
            entity_type="workflow",
            entity_id=candidate.workflow_id,
            event_type="evidence.added",
            before_state=None,
            after_state={
                "workflow_id": str(candidate.workflow_id),
                "organization_id": str(candidate.organization_id),
                "requirement_id": str(candidate.requirement_id),
                "evidence_id": str(evidence.id),
                "status": evidence.status.value,
            },
            actor_reference=submission_actor_reference(str(candidate.id)),
            correlation_id=str(candidate.id),
        )
    )
    session.flush()
    return evidence.id


def _open_link(now: datetime) -> tuple[object, ...]:
    return (
        SubmissionLinkRecord.revoked_at.is_(None),
        SubmissionLinkRecord.expires_at > now,
        SubmissionLinkRecord.use_count < SubmissionLinkRecord.max_uses,
        RequirementRecord.status != RequirementStatus.CANCELLED.value,
        WorkflowRecord.status.in_(
            (WorkflowStatus.DRAFT.value, WorkflowStatus.ACTIVE.value)
        ),
    )


def _invalid_submission(exc: SQLAlchemyError) -> bool:
    origin = getattr(exc, "orig", None)
    sqlstate = getattr(origin, "sqlstate", None) or getattr(origin, "pgcode", None)
    return sqlstate == "23514" and "invalid submission" in str(origin)


def _raise_submission_failure(
    exc: SQLAlchemyError,
    raw_token: str | None = None,
) -> None:
    message = redact_submission_token(redact_connection_secrets(str(exc)))
    if raw_token:
        message = message.replace(raw_token, "[redacted-submission-token]")
    raise PersistenceError("submission persistence failed") from PersistenceError(
        message
    )


def _sqlite_target_organization(
    session: Session,
    token_hash: str,
    now: datetime,
) -> UUID | None:
    return session.scalar(
        select(SubmissionLinkRecord.organization_id)
        .join(
            RequirementRecord,
            RequirementRecord.id == SubmissionLinkRecord.requirement_id,
        )
        .join(
            WorkflowRecord,
            WorkflowRecord.id == SubmissionLinkRecord.workflow_id,
        )
        .where(
            SubmissionLinkRecord.token_hash == token_hash,
            SubmissionLinkRecord.workflow_id == RequirementRecord.workflow_id,
            WorkflowRecord.organization_id == SubmissionLinkRecord.organization_id,
            *_open_link(now),
        )
    )


def _sqlite_reserve(
    session: Session,
    token_hash: str,
    evidence_id: UUID,
    storage_key: str,
    now: datetime,
) -> UUID | None:
    organization_id = _sqlite_target_organization(session, token_hash, now)
    if organization_id is None:
        return None
    if storage_key.split("/")[:2] != [str(organization_id), str(evidence_id)]:
        raise DomainValidationError("The submission is not valid.")
    if _sqlite_key_referenced(session, storage_key) or (
        session.get(EvidenceStorageCleanupRecord, storage_key) is not None
    ):
        raise DomainValidationError("The submission is not valid.")
    session.add(
        EvidenceFileReservationRecord(
            storage_key=storage_key,
            evidence_id=evidence_id,
            organization_id=organization_id,
            reserved_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    session.flush()
    return organization_id


def _sqlite_submit_file(
    session: Session,
    token_hash: str,
    *,
    evidence_id: UUID,
    storage_key: str,
    description: str,
    reference: str | None,
    submitter_name: str | None,
    inspected: InspectedEvidenceFile,
    submitted_at: datetime,
) -> UUID | None:
    candidate = session.execute(
        select(
            SubmissionLinkRecord.id,
            SubmissionLinkRecord.organization_id,
            SubmissionLinkRecord.workflow_id,
            SubmissionLinkRecord.requirement_id,
            SubmissionLinkRecord.use_count,
        )
        .select_from(SubmissionLinkRecord)
        .join(
            RequirementRecord,
            RequirementRecord.id == SubmissionLinkRecord.requirement_id,
        )
        .join(
            WorkflowRecord,
            WorkflowRecord.id == SubmissionLinkRecord.workflow_id,
        )
        .where(
            SubmissionLinkRecord.token_hash == token_hash,
            SubmissionLinkRecord.workflow_id == RequirementRecord.workflow_id,
            WorkflowRecord.organization_id == SubmissionLinkRecord.organization_id,
            *_open_link(submitted_at),
        )
    ).one_or_none()
    if candidate is None:
        return None
    reservation = session.get(EvidenceFileReservationRecord, storage_key)
    if (
        reservation is None
        or reservation.evidence_id != evidence_id
        or reservation.organization_id != candidate.organization_id
        or reservation.expires_at <= submitted_at
        or storage_key.split("/")[:2]
        != [str(candidate.organization_id), str(evidence_id)]
        or session.get(EvidenceStorageCleanupRecord, storage_key) is not None
        or _sqlite_key_referenced(session, storage_key)
    ):
        return None
    updated = session.execute(
        update(SubmissionLinkRecord)
        .where(
            SubmissionLinkRecord.id == candidate.id,
            SubmissionLinkRecord.revoked_at.is_(None),
            SubmissionLinkRecord.expires_at > submitted_at,
            SubmissionLinkRecord.use_count == candidate.use_count,
            SubmissionLinkRecord.use_count < SubmissionLinkRecord.max_uses,
        )
        .values(
            use_count=candidate.use_count + 1,
            last_used_at=submitted_at,
        )
    )
    if updated.rowcount != 1:
        return None
    file_id = uuid4()
    session.add(
        EvidenceRecord(
            id=evidence_id,
            requirement_id=candidate.requirement_id,
            description=description,
            evidence_type=EvidenceType.DOCUMENT.value,
            submitted_by_id=None,
            submitted_at=submitted_at,
            reference=reference,
            status=EvidenceStatus.SUBMITTED.value,
            submitter_name=submitter_name,
            identity_verified=False,
        )
    )
    session.add(
        EvidenceFileRecord(
            id=file_id,
            evidence_id=evidence_id,
            organization_id=candidate.organization_id,
            storage_key=storage_key,
            original_filename=inspected.filename,
            media_type=inspected.media_type,
            byte_size=inspected.byte_size,
            checksum_sha256=inspected.checksum_sha256,
            uploaded_at=submitted_at,
            scan_status="unscanned",
        )
    )
    session.delete(reservation)
    session.add(
        AuditEventRecord(
            occurred_at=submitted_at,
            entity_type="workflow",
            entity_id=candidate.workflow_id,
            event_type="evidence.added",
            before_state=None,
            after_state={
                "workflow_id": str(candidate.workflow_id),
                "organization_id": str(candidate.organization_id),
                "requirement_id": str(candidate.requirement_id),
                "evidence_id": str(evidence_id),
                "evidence_file_id": str(file_id),
                "status": EvidenceStatus.SUBMITTED.value,
                "byte_size": inspected.byte_size,
                "media_type": inspected.media_type,
                "checksum_sha256": inspected.checksum_sha256,
            },
            actor_reference=submission_actor_reference(str(candidate.id)),
            correlation_id=str(candidate.id),
        )
    )
    session.flush()
    return evidence_id


def _sqlite_claim(session: Session, grace_seconds: int) -> tuple[str, UUID] | None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=grace_seconds)
    _sqlite_refuse_malformed_candidates(session, cutoff)
    claimed = _sqlite_claim_cleanup_row(session, cutoff, now)
    if claimed is not None:
        return claimed
    claimed = _sqlite_claim_expired_reservation(session, cutoff, now)
    if claimed is not None:
        return claimed
    stale_before = now - timedelta(seconds=CLAIM_RETRY_SECONDS)
    return _sqlite_claim_stale(session, cutoff, stale_before, now)


def _sqlite_refuse_malformed_candidates(session: Session, cutoff: datetime) -> None:
    keys = list(
        session.scalars(
            select(EvidenceStorageCleanupRecord.storage_key).where(
                EvidenceStorageCleanupRecord.recorded_at <= cutoff
            )
        )
    )
    keys.extend(
        session.scalars(
            select(EvidenceFileReservationRecord.storage_key).where(
                EvidenceFileReservationRecord.expires_at <= cutoff
            )
        )
    )
    for key in keys:
        try:
            require_storage_key(key)
        except DomainValidationError as exc:
            raise PersistenceError(
                "evidence cleanup refused an invalid storage key"
            ) from exc


def _sqlite_claim_cleanup_row(
    session: Session,
    cutoff: datetime,
    now: datetime,
) -> tuple[str, UUID] | None:
    rows = session.scalars(
        select(EvidenceStorageCleanupRecord)
        .where(
            EvidenceStorageCleanupRecord.claimed_at.is_(None),
            EvidenceStorageCleanupRecord.recorded_at <= cutoff,
        )
        .order_by(EvidenceStorageCleanupRecord.recorded_at)
    )
    for row in rows:
        if _sqlite_key_referenced(session, row.storage_key):
            continue
        if _sqlite_reservation_active(session, row.storage_key, now):
            continue
        token = uuid4()
        row.claimed_at = now
        row.claim_token = token
        session.flush()
        return row.storage_key, token
    return None


def _sqlite_claim_expired_reservation(
    session: Session,
    cutoff: datetime,
    now: datetime,
) -> tuple[str, UUID] | None:
    rows = session.scalars(
        select(EvidenceFileReservationRecord)
        .where(EvidenceFileReservationRecord.expires_at <= cutoff)
        .order_by(EvidenceFileReservationRecord.expires_at)
    )
    for row in rows:
        if _sqlite_key_referenced(session, row.storage_key):
            continue
        if session.get(EvidenceStorageCleanupRecord, row.storage_key) is not None:
            continue
        token = uuid4()
        session.add(
            EvidenceStorageCleanupRecord(
                storage_key=row.storage_key,
                organization_id=row.organization_id,
                recorded_at=row.reserved_at,
                claimed_at=now,
                claim_token=token,
            )
        )
        key = row.storage_key
        session.delete(row)
        session.flush()
        return key, token
    return None


def _sqlite_claim_stale(
    session: Session,
    cutoff: datetime,
    stale_before: datetime,
    now: datetime,
) -> tuple[str, UUID] | None:
    rows = session.scalars(
        select(EvidenceStorageCleanupRecord)
        .where(
            EvidenceStorageCleanupRecord.claimed_at.is_not(None),
            EvidenceStorageCleanupRecord.claimed_at <= stale_before,
            EvidenceStorageCleanupRecord.recorded_at <= cutoff,
        )
        .order_by(EvidenceStorageCleanupRecord.claimed_at)
    )
    for row in rows:
        if _sqlite_key_referenced(session, row.storage_key):
            continue
        if _sqlite_reservation_active(session, row.storage_key, now):
            continue
        token = uuid4()
        row.claimed_at = now
        row.claim_token = token
        session.flush()
        return row.storage_key, token
    return None


def _require_storage_object(storage: ObjectStorage, key: str) -> None:
    try:
        storage.exists(key)
    except PersistenceError:
        raise
    except Exception as exc:
        raise PersistenceError(
            "evidence cleanup could not verify object storage"
        ) from exc


def _delete_storage_object(storage: ObjectStorage, key: str) -> None:
    try:
        storage.delete(key)
    except Exception as exc:
        raise PersistenceError(
            "evidence cleanup stopped before an unverified delete"
        ) from exc


def _sqlite_claim_authorized(session: Session, key: str, token: UUID) -> bool:
    now = datetime.now(UTC)
    if _sqlite_key_referenced(session, key):
        return False
    if _sqlite_reservation_active(session, key, now):
        return False
    row = session.get(EvidenceStorageCleanupRecord, key)
    return (
        row is not None
        and row.claim_token is not None
        and UUID(str(row.claim_token)) == UUID(str(token))
    )


def _sqlite_complete_claim(session: Session, key: str, token: UUID) -> None:
    now = datetime.now(UTC)
    if _sqlite_reservation_active(session, key, now):
        raise PersistenceError("evidence cleanup refused an active reservation")
    if _sqlite_key_referenced(session, key):
        raise PersistenceError("evidence cleanup refused a referenced object")
    row = session.get(EvidenceStorageCleanupRecord, key)
    if (
        row is None
        or row.claim_token is None
        or UUID(str(row.claim_token)) != UUID(str(token))
    ):
        raise PersistenceError("evidence cleanup lost ownership of the storage key")
    session.delete(row)
    reservation = session.get(EvidenceFileReservationRecord, key)
    if reservation is not None and reservation.expires_at <= now:
        session.delete(reservation)


def _sqlite_owned_reservation(
    session: Session,
    token: str,
    key: str,
) -> EvidenceFileReservationRecord:
    organization_id = _sqlite_target_organization(
        session,
        submission_token_hash(token),
        datetime.now(UTC),
    )
    reservation = session.get(EvidenceFileReservationRecord, key)
    if (
        organization_id is None
        or reservation is None
        or reservation.organization_id != organization_id
        or key.split("/")[:2]
        != [str(organization_id), str(reservation.evidence_id)]
    ):
        raise PersistenceError("evidence storage operation was not authorized")
    return reservation


def _sqlite_key_referenced(session: Session, key: str) -> bool:
    return (
        session.scalar(
            select(EvidenceFileRecord.id).where(EvidenceFileRecord.storage_key == key)
        )
        is not None
    )


def _sqlite_reservation_active(session: Session, key: str, now: datetime) -> bool:
    reservation = session.get(EvidenceFileReservationRecord, key)
    return reservation is not None and reservation.expires_at > now
