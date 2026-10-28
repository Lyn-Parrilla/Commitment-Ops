"""Application paths cannot cross evidence tenants.

Byte download is not implemented. The member route returns a status only.
Object storage itself is not tenant-scoped: S3 credentials are project-wide.
These tests cover the application checks that run before any object read or
delete.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.domain.evidence_files import build_storage_key, inspect_evidence_file
from app.domain.submissions import generate_submission_token
from app.persistence.exceptions import PersistenceError
from app.persistence.object_storage import FakeObjectStorage
from app.persistence.orm import (
    EvidenceFileRecord,
    EvidenceFileReservationRecord,
    EvidenceStorageCleanupRecord,
    SubmissionLinkRecord,
)
from app.persistence.submissions import SubmissionRepository
from tests.persistence.postgres_support import (
    SKIP_REASON,
    explicit_test_database_url,
    isolated_postgres_engine,
)
from tests.persistence.test_evidence_file_cleanup import _ready
from tests.persistence.test_submission_links import _enter_public, _seed_postgres_link

_VICTIM_BYTES = b"%PDF-1.4\nvictim-synthetic\n"
_ATTACKER_BYTES = b"%PDF-1.4\nattacker-synthetic\n"


def test_other_tenant_cannot_read_or_release_a_reserved_object(tmp_path) -> None:
    victim, attacker, storage, victim_token, attacker_token = _two_tenants(tmp_path)
    evidence_id = uuid4()
    key = _reserved_object(victim, victim_token, evidence_id)
    victim_link = _link(victim, victim.organization_id)
    attacker_link = _link(attacker, attacker.organization_id)
    submitted_id = _submitted_evidence_id(victim)

    assert victim.file_download_status(
        organization_id=attacker.organization_id,
        user_id=attacker.user_id,
        workflow_id=victim_link.workflow_id,
        requirement_id=victim_link.requirement_id,
        evidence_id=submitted_id,
    ) == "missing"
    assert victim.file_download_status(
        organization_id=victim.organization_id,
        user_id=victim.user_id,
        workflow_id=victim_link.workflow_id,
        requirement_id=victim_link.requirement_id,
        evidence_id=submitted_id,
    ) == "unscanned"
    with pytest.raises(PersistenceError, match="not authorized"):
        attacker._release_reservation(attacker_token, key)
    with pytest.raises(PersistenceError, match="not authorized"):
        attacker._record_cleanup(attacker_token, key)

    assert storage.get(key) == _VICTIM_BYTES
    assert _reservation_count(victim, key) == 1
    assert _cleanup_count(victim, key) == 0
    attacker._release_reservation(victim_token, key)
    assert _reservation_count(victim, key) == 0
    assert storage.get(key) == _VICTIM_BYTES
    assert attacker_link.organization_id != victim_link.organization_id


def test_other_tenant_failure_and_cleanup_do_not_remove_the_victim(tmp_path) -> None:
    victim, attacker, storage, victim_token, attacker_token = _two_tenants(tmp_path)
    submitted_key = _only_stored_key(storage, _VICTIM_BYTES)
    reserved_id = uuid4()
    reserved_key = _reserved_object(victim, victim_token, reserved_id)

    def fail(self, *args, **kwargs):
        raise PersistenceError("database failed")

    attacker._commit_file = fail.__get__(attacker, SubmissionRepository)
    with pytest.raises(PersistenceError, match="database failed"):
        attacker.public_submit_file(
            attacker_token,
            description="Attacker file",
            reference=None,
            submitter_name=None,
            inspected=inspect_evidence_file("notes.pdf", _ATTACKER_BYTES),
        )

    assert storage.get(submitted_key) == _VICTIM_BYTES
    assert storage.get(reserved_key) == _VICTIM_BYTES
    assert _ATTACKER_BYTES not in {item.body for item in storage._objects.values()}
    assert victim.cleanup_unreferenced_objects() == 0
    _age_cleanup_row(victim, submitted_key)
    _age_cleanup_row(victim, reserved_key)
    assert victim.cleanup_unreferenced_objects() == 0
    assert storage.get(submitted_key) == _VICTIM_BYTES
    assert storage.get(reserved_key) == _VICTIM_BYTES


@pytest.mark.postgres
def test_postgres_application_role_cannot_cross_evidence_tenants() -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        token_a = generate_submission_token()
        token_b = generate_submission_token()
        tenant_a = _seed_postgres_link(engine, token_a, max_uses=4)
        tenant_b = _seed_postgres_link(engine, token_b, max_uses=4)
        evidence_id = uuid4()
        key = build_storage_key(
            str(tenant_b["organization_id"]),
            str(evidence_id),
            secrets.token_hex(16),
        )
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO organization_memberships (
                        organization_id, user_id, is_active
                    )
                    VALUES (:organization_id, :user_id, true)
                    """
                ),
                [
                    {
                        "organization_id": tenant_a["organization_id"],
                        "user_id": tenant_a["user_id"],
                    },
                    {
                        "organization_id": tenant_b["organization_id"],
                        "user_id": tenant_b["user_id"],
                    },
                ],
            )
            connection.execute(
                text(
                    """
                    SELECT reserve_evidence_object(
                        :token, :evidence_id, :storage_key
                    )
                    """
                ),
                {
                    "token": token_b,
                    "evidence_id": evidence_id,
                    "storage_key": key,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO evidence (
                        id, requirement_id, description, evidence_type,
                        submitted_by_id, submitted_at, reference, status,
                        submitter_name, identity_verified
                    )
                    VALUES (
                        :id, :requirement_id, 'Victim file', 'document',
                        NULL, :submitted_at, NULL, 'submitted', NULL, false
                    )
                    """
                ),
                {
                    "id": evidence_id,
                    "requirement_id": tenant_b["requirement_id"],
                    "submitted_at": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO evidence_files (
                        id, evidence_id, organization_id, storage_key,
                        original_filename, media_type, byte_size,
                        checksum_sha256, uploaded_at, scan_status
                    )
                    VALUES (
                        :id, :evidence_id, :organization_id, :storage_key,
                        'notes.pdf', 'application/pdf', 24,
                        :checksum, :uploaded_at, 'unscanned'
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "evidence_id": evidence_id,
                    "organization_id": tenant_b["organization_id"],
                    "storage_key": key,
                    "checksum": "ab" * 32,
                    "uploaded_at": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO evidence_storage_cleanup (
                        storage_key, organization_id, recorded_at
                    )
                    VALUES (:storage_key, :organization_id, :recorded_at)
                    """
                ),
                {
                    "storage_key": key,
                    "organization_id": tenant_b["organization_id"],
                    "recorded_at": now - timedelta(days=3),
                },
            )

        with engine.connect() as connection:
            transaction = connection.begin()
            _set_member(
                connection,
                tenant_a["user_id"],
                tenant_a["organization_id"],
            )
            visible = connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM evidence_files
                    WHERE evidence_id = :evidence_id
                    """
                ),
                {"evidence_id": evidence_id},
            )
            with pytest.raises(SQLAlchemyError, match="permission denied"):
                connection.execute(text("SELECT storage_key FROM evidence_files"))
            transaction.rollback()
        assert visible == 0

        with engine.connect() as connection:
            transaction = connection.begin()
            _set_member(
                connection,
                tenant_b["user_id"],
                tenant_b["organization_id"],
            )
            status = connection.scalar(
                text(
                    """
                    SELECT scan_status
                    FROM evidence_files
                    WHERE evidence_id = :evidence_id
                    """
                ),
                {"evidence_id": evidence_id},
            )
            transaction.rollback()
        assert status == "unscanned"

        for function_name in (
            "record_evidence_storage_failure",
            "release_evidence_object_reservation",
        ):
            with engine.connect() as connection:
                transaction = connection.begin()
                _enter_public(connection)
                with pytest.raises(SQLAlchemyError, match="invalid submission"):
                    connection.execute(
                        text(f"SELECT {function_name}(:token, :storage_key)"),
                        {"token": token_a, "storage_key": key},
                    )
                transaction.rollback()

        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            with pytest.raises(SQLAlchemyError, match="permission denied"):
                connection.execute(text("DELETE FROM evidence_file_reservations"))
            transaction.rollback()
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            with pytest.raises(SQLAlchemyError, match="permission denied"):
                connection.execute(text("SELECT * FROM claim_evidence_storage_key(60)"))
            transaction.rollback()
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_maintenance"))
            claimed = connection.execute(
                text(
                    "SELECT claimed_storage_key FROM claim_evidence_storage_key(86400)"
                )
            ).one_or_none()
        assert claimed is None
        with engine.begin() as connection:
            remaining = connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM evidence_file_reservations
                    WHERE storage_key = :storage_key
                    """
                ),
                {"storage_key": key},
            )
            cleanup = connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM evidence_storage_cleanup
                    WHERE storage_key = :storage_key
                    """
                ),
                {"storage_key": key},
            )
        assert remaining == 1
        assert cleanup == 1


class _Tenant:
    def __init__(self, repository: SubmissionRepository, organization_id, user_id):
        self.repository = repository
        self.organization_id = organization_id
        self.user_id = user_id

    def file_download_status(self, **kwargs):
        return self.repository.file_download_status(**kwargs)

    def _release_reservation(self, token: str, key: str) -> None:
        self.repository._release_reservation(token, key)

    def _record_cleanup(self, token: str, key: str) -> None:
        self.repository._record_cleanup(token, key)

    def public_submit_file(self, *args, **kwargs):
        return self.repository.public_submit_file(*args, **kwargs)

    def cleanup_unreferenced_objects(self) -> int:
        return self.repository.cleanup_unreferenced_objects()

    @property
    def _session_factory(self):
        return self.repository._session_factory

    @property
    def _commit_file(self):
        return self.repository._commit_file

    @_commit_file.setter
    def _commit_file(self, value) -> None:
        self.repository._commit_file = value


def _two_tenants(tmp_path):
    storage = FakeObjectStorage()
    victim_org = uuid4()
    attacker_org = uuid4()
    victim_repo, _, victim_token = _ready(
        tmp_path,
        storage,
        organization_id=victim_org,
    )
    attacker_repo, _, attacker_token = _ready(
        tmp_path,
        storage,
        organization_id=attacker_org,
    )
    victim_repo.public_submit_file(
        victim_token,
        description="Victim file",
        reference=None,
        submitter_name=None,
        inspected=inspect_evidence_file("notes.pdf", _VICTIM_BYTES),
    )
    victim = _Tenant(victim_repo, victim_org, _member_id(victim_repo, victim_org))
    attacker = _Tenant(
        attacker_repo,
        attacker_org,
        _member_id(attacker_repo, attacker_org),
    )
    return victim, attacker, storage, victim_token, attacker_token


def _reserved_object(victim: _Tenant, token: str, evidence_id) -> str:
    key = build_storage_key(
        str(victim.organization_id),
        str(evidence_id),
        secrets.token_hex(16),
    )
    reserved = victim.repository._reserve(token, evidence_id, key)
    assert reserved == victim.organization_id
    victim.repository._storage.put_new(key, _VICTIM_BYTES, "application/pdf")
    return key


def _member_id(repository: SubmissionRepository, organization_id):
    from app.persistence.orm import OrganizationMembershipRecord

    with repository._session_factory() as session:
        return session.scalar(
            select(OrganizationMembershipRecord.user_id).where(
                OrganizationMembershipRecord.organization_id == organization_id
            )
        )


def _link(tenant: _Tenant, organization_id) -> _LinkIds:
    with tenant._session_factory() as session:
        row = session.scalars(
            select(SubmissionLinkRecord).where(
                SubmissionLinkRecord.organization_id == organization_id
            )
        ).one()
        return _LinkIds(
            organization_id=row.organization_id,
            workflow_id=row.workflow_id,
            requirement_id=row.requirement_id,
        )


def _submitted_evidence_id(tenant: _Tenant):
    with tenant._session_factory() as session:
        return session.scalars(select(EvidenceFileRecord.evidence_id)).one()


def _only_stored_key(storage: FakeObjectStorage, body: bytes) -> str:
    matches = [key for key, item in storage._objects.items() if item.body == body]
    assert len(matches) == 1
    return matches[0]


def _reservation_count(tenant: _Tenant, key: str) -> int:
    with tenant._session_factory() as session:
        return session.scalar(
            select(EvidenceFileReservationRecord.storage_key).where(
                EvidenceFileReservationRecord.storage_key == key
            )
        ) is not None and 1 or 0


def _cleanup_count(tenant: _Tenant, key: str) -> int:
    with tenant._session_factory() as session:
        row = session.scalar(
            select(EvidenceStorageCleanupRecord.storage_key).where(
                EvidenceStorageCleanupRecord.storage_key == key
            )
        )
    return 0 if row is None else 1


def _age_cleanup_row(tenant: _Tenant, key: str) -> None:
    with tenant._session_factory.begin() as session:
        existing = session.get(EvidenceStorageCleanupRecord, key)
        if existing is None:
            session.add(
                EvidenceStorageCleanupRecord(
                    storage_key=key,
                    organization_id=tenant.organization_id,
                    recorded_at=datetime.now(UTC) - timedelta(days=3),
                )
            )
        else:
            existing.recorded_at = datetime.now(UTC) - timedelta(days=3)
            existing.claimed_at = None
            existing.claim_token = None


def _set_member(connection, user_id, organization_id) -> None:
    connection.execute(text("SET LOCAL ROLE commitmentops_app"))
    connection.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": str(user_id)},
    )
    connection.execute(
        text("SELECT set_config('app.organization_id', :organization_id, true)"),
        {"organization_id": str(organization_id)},
    )


@dataclass(frozen=True)
class _LinkIds:
    organization_id: UUID
    workflow_id: UUID
    requirement_id: UUID
