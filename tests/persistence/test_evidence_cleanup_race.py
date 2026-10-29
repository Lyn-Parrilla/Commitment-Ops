"""A stale cleanup worker cannot delete after its claim token changes."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.domain.evidence_files import build_storage_key
from app.persistence import create_session_factory
from app.persistence.exceptions import PersistenceError
from app.persistence.object_storage import FakeObjectStorage
from app.persistence.orm import (
    EvidenceFileRecord,
    EvidenceRecord,
    EvidenceStorageCleanupRecord,
    RequirementRecord,
    SubmissionLinkRecord,
)
from app.persistence.submissions import SubmissionRepository
from tests.persistence.postgres_support import (
    SKIP_REASON,
    explicit_test_database_url,
    isolated_postgres_engine,
)
from tests.persistence.test_evidence_file_cleanup import (
    _orphan,
    _ready,
    uuid_from_key,
)

_BODY = b"%PDF-1.4\nstale-worker\n"


def test_rotated_token_prevents_the_stale_worker_from_deleting(tmp_path) -> None:
    repository, storage, key = _claimed_orphan(tmp_path)

    def rotate(storage_key: str) -> bool:
        _replace_claim_token(repository, storage_key)
        return True

    storage.exists = rotate
    assert repository.cleanup_unreferenced_objects() == 0
    assert storage.get(key) == _BODY
    assert _cleanup_count(repository, key) == 1


def test_unchanged_token_deletes_the_object_and_completes_the_row(tmp_path) -> None:
    repository, storage, key = _claimed_orphan(tmp_path)

    assert repository.cleanup_unreferenced_objects() == 1
    with pytest.raises(PersistenceError, match="not found"):
        storage.get(key)
    assert _cleanup_count(repository, key) == 0


def test_evidence_inserted_after_claim_prevents_deletion(tmp_path) -> None:
    repository, storage, key = _claimed_orphan(tmp_path)

    def plant(storage_key: str) -> bool:
        _insert_evidence_file(repository._session_factory, storage_key)
        return True

    storage.exists = plant
    assert repository.cleanup_unreferenced_objects() == 0
    assert storage.get(key) == _BODY
    assert _cleanup_count(repository, key) == 1


def test_sqlite_stale_token_rotation_leaves_the_object_untouched(tmp_path) -> None:
    repository, storage, key = _claimed_orphan(tmp_path)
    original = _claim_token(repository, key)

    def rotate(storage_key: str) -> bool:
        _replace_claim_token(repository, storage_key)
        return True

    storage.exists = rotate
    assert repository.cleanup_unreferenced_objects() == 0
    assert storage.get(key) == _BODY
    assert _claim_token(repository, key) != original


def test_completion_failure_after_delete_keeps_the_cleanup_row(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, storage, key = _claimed_orphan(tmp_path)

    def fail_completion(session, storage_key: str, token) -> None:
        raise RuntimeError("completion failed")

    monkeypatch.setattr(
        "app.persistence.submissions._sqlite_complete_claim",
        fail_completion,
    )
    with pytest.raises(RuntimeError, match="completion failed"):
        repository.cleanup_unreferenced_objects()

    with pytest.raises(PersistenceError, match="not found"):
        storage.get(key)
    assert _cleanup_count(repository, key) == 1


def test_cleanup_exception_does_not_complete_the_claim(tmp_path) -> None:
    repository, storage, key = _claimed_orphan(tmp_path)

    def fail(storage_key: str) -> None:
        raise RuntimeError("disk failed")

    storage.delete = fail
    with pytest.raises(PersistenceError, match="unverified delete"):
        repository.cleanup_unreferenced_objects()

    assert storage.get(key) == _BODY
    assert _cleanup_count(repository, key) == 1


@pytest.mark.postgres
def test_postgres_rotated_token_and_evidence_row_block_deletion() -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        repository, storage, key = _postgres_orphan(engine)
        storage.exists = lambda storage_key: _rotate_postgres(engine, storage_key)
        assert repository.cleanup_unreferenced_objects() == 0
        assert storage.get(key) == _BODY

        repository, storage, key = _postgres_orphan(engine)
        storage.exists = lambda storage_key: _plant_postgres(engine, storage_key)
        assert repository.cleanup_unreferenced_objects() == 0
        assert storage.get(key) == _BODY

        repository, storage, key = _postgres_orphan(engine)
        assert repository.cleanup_unreferenced_objects() == 1
        with pytest.raises(PersistenceError, match="not found"):
            storage.get(key)
        with engine.connect() as connection:
            remaining = connection.scalar(
                text(
                    "SELECT count(*) FROM evidence_storage_cleanup "
                    "WHERE storage_key = :storage_key"
                ),
                {"storage_key": key},
            )
        assert remaining == 0


@pytest.mark.postgres
def test_second_worker_waits_and_cannot_reclaim_during_the_lock() -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        repository, storage, key = _postgres_orphan(engine)
        release = threading.Event()

        def block_delete(storage_key: str) -> None:
            release.wait(timeout=10)
            storage._objects.pop(storage_key, None)

        storage.delete = block_delete
        worker = threading.Thread(
            target=repository.cleanup_unreferenced_objects,
            name="cleanup-holder",
        )
        worker.start()
        holder_pid = _wait_for_advisory_lock(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE evidence_storage_cleanup
                       SET claimed_at = clock_timestamp() - interval '6 minutes'
                     WHERE storage_key = :storage_key
                    """
                ),
                {"storage_key": key},
            )
            held_token = connection.scalar(
                text(
                    "SELECT claim_token FROM evidence_storage_cleanup "
                    "WHERE storage_key = :storage_key"
                ),
                {"storage_key": key},
            )
        outcome: dict[str, object] = {}

        def reclaim() -> None:
            with engine.connect() as connection:
                with connection.begin():
                    connection.execute(text("SET LOCAL lock_timeout = '8s'"))
                    connection.execute(
                        text("SET LOCAL ROLE commitmentops_maintenance")
                    )
                    outcome["row"] = connection.execute(
                        text(
                            "SELECT claimed_storage_key FROM "
                            "claim_evidence_storage_key(86400)"
                        )
                    ).one_or_none()

        reclaimer = threading.Thread(target=reclaim, name="cleanup-reclaimer")
        reclaimer.start()
        assert _reclaimer_is_blocked(engine, holder_pid)
        with engine.connect() as connection:
            current_token = connection.scalar(
                text(
                    "SELECT claim_token FROM evidence_storage_cleanup "
                    "WHERE storage_key = :storage_key"
                ),
                {"storage_key": key},
            )
        assert current_token == held_token
        release.set()
        worker.join(timeout=10)
        reclaimer.join(timeout=10)
        assert worker.is_alive() is False
        assert reclaimer.is_alive() is False
        assert outcome["row"] is None
        with pytest.raises(PersistenceError, match="not found"):
            storage.get(key)
        assert _advisory_lock_count(engine) == 0


@pytest.mark.postgres
def test_postgres_delete_failure_releases_the_advisory_lock() -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        repository, storage, key = _postgres_orphan(engine)

        def fail(storage_key: str) -> None:
            raise RuntimeError("disk failed")

        storage.delete = fail
        with pytest.raises(PersistenceError, match="unverified delete"):
            repository.cleanup_unreferenced_objects()
        assert storage.get(key) == _BODY
        with engine.connect() as connection:
            remaining = connection.scalar(
                text(
                    "SELECT count(*) FROM evidence_storage_cleanup "
                    "WHERE storage_key = :storage_key"
                ),
                {"storage_key": key},
            )
        assert remaining == 1
        assert _advisory_lock_count(engine) == 0


@pytest.mark.postgres
def test_postgres_completion_failure_releases_the_lock_and_keeps_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        repository, storage, key = _postgres_orphan(engine)

        def fail_completion(self, connection, storage_key: str, token) -> None:
            raise RuntimeError("completion failed")

        monkeypatch.setattr(
            SubmissionRepository,
            "_postgres_complete",
            fail_completion,
        )
        with pytest.raises(RuntimeError, match="completion failed"):
            repository.cleanup_unreferenced_objects()
        with pytest.raises(PersistenceError, match="not found"):
            storage.get(key)
        with engine.connect() as connection:
            remaining = connection.scalar(
                text(
                    "SELECT count(*) FROM evidence_storage_cleanup "
                    "WHERE storage_key = :storage_key"
                ),
                {"storage_key": key},
            )
        assert remaining == 1
        assert _advisory_lock_count(engine) == 0


def _claimed_orphan(tmp_path):
    repository, storage, _token = _ready(tmp_path)
    key = _orphan(storage, days_old=2, cleanup=True)
    storage._objects.pop(key)
    storage.put_new(key, _BODY, "application/pdf")
    with repository._session_factory() as session:
        session.add(
            EvidenceStorageCleanupRecord(
                storage_key=key,
                organization_id=uuid_from_key(key),
                recorded_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        session.commit()
    return repository, storage, key


def _postgres_orphan(engine):
    storage = FakeObjectStorage()
    key = build_storage_key(str(uuid4()), str(uuid4()), uuid4().hex)
    storage.put_new(key, _BODY, "application/pdf")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO evidence_storage_cleanup (
                    storage_key, organization_id, recorded_at
                )
                VALUES (
                    :storage_key, :organization_id,
                    clock_timestamp() - interval '2 days'
                )
                """
            ),
            {"storage_key": key, "organization_id": uuid_from_key(key)},
        )
    repository = SubmissionRepository(create_session_factory(engine), storage)
    return repository, storage, key


def _replace_claim_token(repository: SubmissionRepository, key: str) -> None:
    with repository._session_factory.begin() as session:
        row = session.get(EvidenceStorageCleanupRecord, key)
        assert row is not None
        row.claim_token = uuid4()


def _claim_token(repository: SubmissionRepository, key: str):
    with repository._session_factory() as session:
        return session.scalar(
            select(EvidenceStorageCleanupRecord.claim_token).where(
                EvidenceStorageCleanupRecord.storage_key == key
            )
        )


def _cleanup_count(repository: SubmissionRepository, key: str) -> int:
    with repository._session_factory() as session:
        row = session.get(EvidenceStorageCleanupRecord, key)
    return 0 if row is None else 1


def _insert_evidence_file(session_factory, key: str) -> None:
    with session_factory.begin() as session:
        requirement_id = session.scalar(select(RequirementRecord.id))
        organization_id = session.scalar(
            select(SubmissionLinkRecord.organization_id)
        )
        evidence_id = uuid4()
        session.add(
            EvidenceRecord(
                id=evidence_id,
                requirement_id=requirement_id,
                description="Synthetic file",
                evidence_type="document",
                submitted_by_id=None,
                submitted_at=datetime.now(UTC),
                reference=None,
                status="submitted",
                submitter_name=None,
                identity_verified=False,
            )
        )
        session.add(
            EvidenceFileRecord(
                id=uuid4(),
                evidence_id=evidence_id,
                organization_id=organization_id,
                storage_key=key,
                original_filename="notes.pdf",
                media_type="application/pdf",
                byte_size=len(_BODY),
                checksum_sha256="ab" * 32,
                uploaded_at=datetime.now(UTC),
                scan_status="unscanned",
            )
        )


def _rotate_postgres(engine, key: str) -> bool:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE evidence_storage_cleanup
                   SET claim_token = :claim_token
                 WHERE storage_key = :storage_key
                """
            ),
            {"claim_token": uuid4(), "storage_key": key},
        )
    return True


def _plant_postgres(engine, key: str) -> bool:
    with engine.begin() as connection:
        organization_id = connection.scalar(
            text("SELECT id FROM organizations ORDER BY id LIMIT 1")
        )
        person_id = uuid4()
        workflow_id = uuid4()
        requirement_id = uuid4()
        evidence_id = uuid4()
        connection.execute(
            text(
                """
                INSERT INTO people (id, name, email)
                VALUES (:id, 'Cleanup owner', :email)
                """
            ),
            {"id": person_id, "email": f"{person_id}@example.test"},
        )
        connection.execute(
            text(
                """
                INSERT INTO workflows (
                    id, organization_id, name, workflow_type, status
                )
                VALUES (
                    :id, :organization_id, 'Cleanup workflow', 'general', 'active'
                )
                """
            ),
            {"id": workflow_id, "organization_id": organization_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO requirements (
                    id, workflow_id, title, description, owner_id,
                    evidence_expectation, provenance, status, review_status
                )
                VALUES (
                    :id, :workflow_id, 'Cleanup requirement', 'Synthetic',
                    :owner_id, 'A file', 'user_provided', 'not_started',
                    'approved'
                )
                """
            ),
            {
                "id": requirement_id,
                "workflow_id": workflow_id,
                "owner_id": person_id,
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
                    :id, :requirement_id, 'Synthetic file', 'document',
                    NULL, clock_timestamp(), NULL, 'submitted', NULL, false
                )
                """
            ),
            {"id": evidence_id, "requirement_id": requirement_id},
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
                    'notes.pdf', 'application/pdf', :byte_size,
                    :checksum, clock_timestamp(), 'unscanned'
                )
                """
            ),
            {
                "id": uuid4(),
                "evidence_id": evidence_id,
                "organization_id": organization_id,
                "storage_key": key,
                "byte_size": len(_BODY),
                "checksum": "ab" * 32,
            },
        )
    return True


def _wait_for_advisory_lock(engine) -> int:
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while datetime.now(UTC) < deadline:
        with engine.connect() as connection:
            pid = connection.scalar(
                text(
                    """
                    SELECT pid
                    FROM pg_locks
                    WHERE locktype = 'advisory' AND granted
                    LIMIT 1
                    """
                )
            )
        if pid is not None:
            return int(pid)
        threading.Event().wait(0.05)
    raise AssertionError("cleanup worker did not acquire the advisory lock")


def _reclaimer_is_blocked(engine, holder_pid: int) -> bool:
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while datetime.now(UTC) < deadline:
        with engine.connect() as connection:
            blocked = connection.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_stat_activity AS activity
                        WHERE activity.wait_event_type = 'Lock'
                          AND :holder_pid = ANY(pg_blocking_pids(activity.pid))
                    )
                    """
                ),
                {"holder_pid": holder_pid},
            )
        if blocked:
            return True
        threading.Event().wait(0.05)
    return False


def _advisory_lock_count(engine) -> int:
    with engine.connect() as connection:
        count = connection.scalar(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
        )
    return int(count or 0)
