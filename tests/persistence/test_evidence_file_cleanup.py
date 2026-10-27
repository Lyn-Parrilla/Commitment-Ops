"""File upload failure and orphan cleanup on a temporary database."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.domain import (
    Person,
    Requirement,
    RequirementProvenance,
    Workflow,
    WorkflowType,
)
from app.domain.evidence_files import build_storage_key, inspect_evidence_file
from app.domain.exceptions import DomainValidationError
from app.domain.submissions import generate_submission_token, submission_token_hash
from app.persistence import (
    Base,
    Organization,
    OrganizationAdmin,
    WorkflowRepository,
    create_database_engine,
    create_session_factory,
)
from app.persistence.exceptions import PersistenceError
from app.persistence.object_storage import (
    FakeObjectStorage,
    S3CompatibleObjectStorage,
)
from tests.persistence.test_object_storage import FakeS3Client
from app.persistence.orm import (
    EvidenceFileReservationRecord,
    EvidenceStorageCleanupRecord,
    SubmissionLinkRecord,
)
from app.persistence.submissions import CLEANUP_GRACE_SECONDS, SubmissionRepository
from sqlalchemy import select


def test_database_failure_after_upload_removes_the_object(tmp_path) -> None:
    repository, storage, token = _ready(tmp_path)

    def fail(self, *args, **kwargs):
        raise PersistenceError("database failed")

    repository._commit_file = fail.__get__(repository, SubmissionRepository)
    with pytest.raises(PersistenceError):
        repository.public_submit_file(
            token,
            description="Synthetic file",
            reference=None,
            submitter_name=None,
            inspected=inspect_evidence_file("notes.pdf", b"%PDF-1.4\n"),
        )

    assert storage._objects == {}
    assert _use_count(repository) == 0


def test_s3_client_removes_the_object_after_a_database_failure(tmp_path) -> None:
    client = FakeS3Client()

    def forbid(*args, **kwargs):
        raise AssertionError("storage client contacted an endpoint")

    storage = S3CompatibleObjectStorage(
        endpoint="https://storage.example.test",
        region="auto",
        bucket="evidence",
        access_key_id="scoped-access-key",
        secret_access_key="scoped-secret",
        client=client,
        client_factory=forbid,
    )
    repository, _, token = _ready(tmp_path, storage)

    def fail(self, *args, **kwargs):
        raise PersistenceError("database failed")

    repository._commit_file = fail.__get__(repository, SubmissionRepository)
    with pytest.raises(PersistenceError, match="database failed"):
        repository.public_submit_file(
            token,
            description="Synthetic file",
            reference=None,
            submitter_name=None,
            inspected=inspect_evidence_file("notes.pdf", b"%PDF-1.4\n"),
        )

    assert client.objects == {}
    assert _use_count(repository) == 0


def test_failed_object_delete_records_a_cleanup_key(tmp_path) -> None:
    repository, storage, token = _ready(tmp_path)

    def fail_commit(self, *args, **kwargs):
        raise PersistenceError("database failed")

    def fail_delete(key: str) -> None:
        raise PersistenceError("delete failed")

    repository._commit_file = fail_commit.__get__(repository, SubmissionRepository)
    storage.delete = fail_delete
    with pytest.raises(PersistenceError, match="database failed"):
        repository.public_submit_file(
            token,
            description="Synthetic file",
            reference=None,
            submitter_name=None,
            inspected=inspect_evidence_file("notes.pdf", b"%PDF-1.4\n"),
        )

    assert len(storage._objects) == 1
    with repository._session_factory() as session:
        assert session.scalars(select(EvidenceStorageCleanupRecord)).one()


def test_cleanup_deletes_only_old_unreferenced_keys(tmp_path) -> None:
    repository, storage, token = _ready(tmp_path)
    evidence_id = repository.public_submit_file(
        token,
        description="Synthetic file",
        reference=None,
        submitter_name=None,
        inspected=inspect_evidence_file("notes.pdf", b"%PDF-1.4\n"),
    )
    assert evidence_id is not None
    referenced = next(iter(storage._objects))
    old_key = _orphan(storage, days_old=2, cleanup=True)
    recent_key = _orphan(storage, days_old=0, cleanup=True)
    reserved_key = _orphan(storage, days_old=0, cleanup=False, reserved_hours=1)
    expired_key = _orphan(storage, days_old=2, cleanup=False, reserved_hours=-48)

    with repository._session_factory() as session:
        session.add_all(
            [
                EvidenceStorageCleanupRecord(
                    storage_key=old_key,
                    organization_id=uuid_from_key(old_key),
                    recorded_at=datetime.now(UTC) - timedelta(days=2),
                ),
                EvidenceStorageCleanupRecord(
                    storage_key=recent_key,
                    organization_id=uuid_from_key(recent_key),
                    recorded_at=datetime.now(UTC),
                ),
                EvidenceFileReservationRecord(
                    storage_key=reserved_key,
                    evidence_id=uuid_from_key(reserved_key, 1),
                    organization_id=uuid_from_key(reserved_key),
                    reserved_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
                EvidenceFileReservationRecord(
                    storage_key=expired_key,
                    evidence_id=uuid_from_key(expired_key, 1),
                    organization_id=uuid_from_key(expired_key),
                    reserved_at=datetime.now(UTC) - timedelta(days=3),
                    expires_at=datetime.now(UTC) - timedelta(hours=48),
                ),
            ]
        )
        session.commit()

    with pytest.raises(PersistenceError, match="too short"):
        repository.cleanup_unreferenced_objects(grace_seconds=60)
    assert storage.exists(old_key) is True

    deleted = repository.cleanup_unreferenced_objects()

    assert deleted == 2
    assert storage.exists(referenced) is True
    assert storage.exists(recent_key) is True
    assert storage.exists(reserved_key) is True
    assert storage.exists(old_key) is False
    assert storage.exists(expired_key) is False
    assert _use_count(repository) == 1


def test_cleanup_fails_closed_when_storage_cannot_be_checked(tmp_path) -> None:
    repository, storage, _token = _ready(tmp_path)
    key = _orphan(storage, days_old=2, cleanup=True)
    with repository._session_factory() as session:
        session.add(
            EvidenceStorageCleanupRecord(
                storage_key=key,
                organization_id=uuid_from_key(key),
                recorded_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        session.commit()

    def unavailable(storage_key: str) -> bool:
        raise RuntimeError("storage down")

    storage.exists = unavailable
    with pytest.raises(PersistenceError, match="could not verify"):
        repository.cleanup_unreferenced_objects()
    assert key in storage._objects


def test_cleanup_claims_before_delete_and_keeps_the_claim_on_failure(tmp_path) -> None:
    repository, storage, _token = _ready(tmp_path)
    key = _orphan(storage, days_old=2, cleanup=True)
    with repository._session_factory() as session:
        session.add(
            EvidenceStorageCleanupRecord(
                storage_key=key,
                organization_id=uuid_from_key(key),
                recorded_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        session.commit()
    seen: list[str] = []

    def fail_delete(storage_key: str) -> None:
        with repository._session_factory() as session:
            row = session.get(EvidenceStorageCleanupRecord, storage_key)
            assert row is not None
            assert row.claimed_at is not None
            assert row.claim_token is not None
        seen.append(storage_key)
        raise RuntimeError("disk failed")

    storage.delete = fail_delete
    with pytest.raises(PersistenceError, match="unverified delete"):
        repository.cleanup_unreferenced_objects()

    assert seen == [key]
    assert key in storage._objects
    with repository._session_factory() as session:
        row = session.get(EvidenceStorageCleanupRecord, key)
        assert row is not None and row.claimed_at is not None


def test_reserve_rejects_a_key_queued_for_cleanup(tmp_path) -> None:
    repository, _storage, token = _ready(tmp_path)
    with repository._session_factory() as session:
        organization_id = session.scalars(
            select(SubmissionLinkRecord.organization_id)
        ).one()
    evidence_id = uuid4()
    key = build_storage_key(str(organization_id), str(evidence_id), "ab" * 16)
    with repository._session_factory() as session:
        session.add(
            EvidenceStorageCleanupRecord(
                storage_key=key,
                organization_id=organization_id,
                recorded_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        session.commit()

    with pytest.raises(DomainValidationError):
        repository._reserve(token, evidence_id, key)
    assert _use_count(repository) == 0


def test_cleanup_completion_rejects_an_active_reservation(tmp_path) -> None:
    repository, storage, _token = _ready(tmp_path)
    key = _orphan(storage, days_old=2, cleanup=True)
    with repository._session_factory() as session:
        session.add(
            EvidenceStorageCleanupRecord(
                storage_key=key,
                organization_id=uuid_from_key(key),
                recorded_at=datetime.now(UTC) - timedelta(days=2),
                claimed_at=datetime.now(UTC),
                claim_token=uuid4(),
            )
        )
        reservation_token = session.scalars(
            select(EvidenceStorageCleanupRecord.claim_token)
        ).one()
        session.add(
            EvidenceFileReservationRecord(
                storage_key=key,
                evidence_id=uuid_from_key(key, 1),
                organization_id=uuid_from_key(key),
                reserved_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.commit()

    with pytest.raises(PersistenceError, match="active reservation"):
        repository._complete_claim(key, reservation_token)
    assert key in storage._objects
    with repository._session_factory() as session:
        assert session.get(EvidenceStorageCleanupRecord, key) is not None
        assert session.get(EvidenceFileReservationRecord, key) is not None


def test_cleanup_refuses_a_malformed_candidate_without_deleting(tmp_path) -> None:
    repository, storage, _token = _ready(tmp_path)
    key = "a" * 106
    storage._objects[key] = storage._objects.get(key)
    with repository._session_factory() as session:
        session.add(
            EvidenceStorageCleanupRecord(
                storage_key=key,
                organization_id=uuid4(),
                recorded_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        session.commit()

    with pytest.raises(PersistenceError):
        repository.cleanup_unreferenced_objects()
    assert key in storage._objects


def _ready(
    tmp_path,
    storage: FakeObjectStorage | S3CompatibleObjectStorage | None = None,
    *,
    organization_id: UUID | None = None,
):
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'files.db'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    organization_id = organization_id or uuid4()
    user_id = uuid4()
    OrganizationAdmin(session_factory).save(
        Organization(id=organization_id, name="Synthetic Organization")
    )
    OrganizationAdmin(session_factory).set_membership(
        organization_id=organization_id,
        user_id=user_id,
        is_active=True,
    )
    owner = Person.create(name="Synthetic Owner", email="owner@example.org")
    workflow = Workflow.create(
        name="Synthetic workflow",
        workflow_type=WorkflowType.GENERAL,
        organization_id=organization_id,
    )
    requirement = Requirement.create(
        title="Synthetic requirement",
        description="A synthetic requirement",
        owner=owner,
        evidence_expectation="A synthetic record",
        provenance=RequirementProvenance.USER_PROVIDED,
    )
    workflow.add_requirement(requirement)
    WorkflowRepository(session_factory).save(
        workflow,
        user_id=user_id,
        organization_id=organization_id,
        actor_reference=str(user_id),
    )
    if storage is None:
        storage = FakeObjectStorage()
    repository = SubmissionRepository(session_factory, storage)
    token = generate_submission_token()
    repository.create_link(
        organization_id=organization_id,
        workflow_id=workflow.id,
        requirement_id=requirement.id,
        token_hash=submission_token_hash(token),
        created_by_user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(days=14),
        max_uses=25,
        user_id=user_id,
    )
    return repository, storage, token


def _use_count(repository: SubmissionRepository) -> int:
    with repository._session_factory() as session:
        return session.scalars(select(SubmissionLinkRecord.use_count)).one()


def _orphan(
    storage: FakeObjectStorage,
    *,
    days_old: int,
    cleanup: bool,
    reserved_hours: int | None = None,
) -> str:
    del days_old, cleanup, reserved_hours
    key = build_storage_key(str(uuid4()), str(uuid4()), uuid4().hex)
    storage.put_new(key, b"%PDF-1.4\n", "application/pdf")
    return key


def uuid_from_key(key: str, index: int = 0):
    from uuid import UUID

    return UUID(key.split("/")[index])
