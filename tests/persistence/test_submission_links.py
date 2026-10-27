"""Submission-link races and the PostgreSQL RLS boundary."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.persistence.database import quoted_isolated_schema
from app.persistence.exceptions import PersistenceError
from app.persistence.rls import APP_ROLE

from app.domain import (
    EvidenceType,
    Person,
    Requirement,
    RequirementProvenance,
    Workflow,
    WorkflowType,
)
from app.domain.evidence_files import build_storage_key
from app.domain.submissions import generate_submission_token, submission_token_hash
from app.persistence.object_storage import FakeObjectStorage
from app.persistence import (
    Base,
    Organization,
    OrganizationAdmin,
    WorkflowRepository,
    create_database_engine,
    create_session_factory,
)
from app.persistence.submissions import SubmissionRepository
from tests.persistence.postgres_support import (
    SKIP_REASON,
    explicit_test_database_url,
    isolated_postgres_engine,
    require_test_target,
)


def test_concurrent_submissions_stop_at_the_use_limit(tmp_path) -> None:
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'submission-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    organization_id = uuid4()
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
    raw_token = generate_submission_token()
    submissions = SubmissionRepository(session_factory)
    submissions.create_link(
        organization_id=organization_id,
        workflow_id=workflow.id,
        requirement_id=requirement.id,
        token_hash=submission_token_hash(raw_token),
        created_by_user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        max_uses=1,
        user_id=user_id,
    )
    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(
                submissions.public_submit(
                    raw_token,
                    description="Synthetic concurrent receipt",
                    reference="https://example.test/concurrent",
                    submitter_name=None,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    engine.dispose()
    assert errors == []
    assert sorted(result is not None for result in results) == [False, True]


def test_database_rejects_clearing_a_revocation(tmp_path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'revoke.db'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    organization_id = uuid4()
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
    submissions = SubmissionRepository(session_factory)
    link = submissions.create_link(
        organization_id=organization_id,
        workflow_id=workflow.id,
        requirement_id=requirement.id,
        token_hash=submission_token_hash(generate_submission_token()),
        created_by_user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        max_uses=1,
        user_id=user_id,
    )
    revoked = submissions.revoke_link(
        organization_id=organization_id,
        workflow_id=workflow.id,
        requirement_id=requirement.id,
        link_id=link.id,
        user_id=user_id,
    )
    with pytest.raises(SQLAlchemyError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE submission_links SET revoked_at = NULL")
            )
    with engine.connect() as connection:
        stored = connection.scalar(
            text("SELECT revoked_at FROM submission_links")
        )
    engine.dispose()
    assert revoked is not None and revoked.revoked_at is not None
    assert stored is not None


@pytest.mark.postgres
def test_public_submission_function_cannot_cross_the_tenant_boundary() -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        raw_token = generate_submission_token()
        seeded = _seed_postgres_link(engine, raw_token)
        with engine.begin() as connection:
            _assert_function_boundary(connection)
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            connection.execute(
                text("SELECT set_config('app.user_id', '', true)")
            )
            connection.execute(
                text("SELECT set_config('app.organization_id', '', true)")
            )
            assert connection.scalar(text("SELECT COUNT(*) FROM workflows")) == 0
            assert connection.scalar(text("SELECT COUNT(*) FROM evidence")) == 0
            assert connection.scalar(
                text("SELECT COUNT(*) FROM submission_links")
            ) == 0
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text("SELECT token_hash FROM submission_links")
                    )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            """
                            INSERT INTO evidence (
                                id, requirement_id, description, evidence_type,
                                submitted_by_id, submitted_at, status,
                                identity_verified
                            )
                            VALUES (
                                :id, :requirement_id, 'smuggled', 'note',
                                NULL, CURRENT_TIMESTAMP, 'submitted', false
                            )
                            """
                        ),
                        {
                            "id": uuid4(),
                            "requirement_id": seeded["requirement_id"],
                        },
                    )
            hidden = connection.execute(
                text(
                    "UPDATE requirements SET status = 'completed' "
                    "WHERE id = :requirement_id"
                ),
                {"requirement_id": seeded["requirement_id"]},
            )
            assert hidden.rowcount == 0
            context_rows = connection.execute(
                text(
                    """
                    SELECT
                        organization_name,
                        requirement_title,
                        evidence_expectation
                    FROM public_submission_context(:token)
                    """
                ),
                {"token": raw_token},
            ).all()
            hashed_context = connection.execute(
                text(
                    "SELECT requirement_title "
                    "FROM public_submission_context(:token)"
                ),
                {"token": submission_token_hash(raw_token)},
            ).all()
            unknown_context = connection.execute(
                text(
                    "SELECT requirement_title "
                    "FROM public_submission_context(:token)"
                ),
                {"token": "0" * 64},
            ).all()
            assert len(context_rows) == 1
            assert context_rows[0].requirement_title == "Synthetic requirement"
            assert context_rows[0].evidence_expectation == "A synthetic record"
            assert "Other requirement" not in context_rows[0].requirement_title
            assert hashed_context == []
            assert unknown_context == []
            hash_replay = connection.scalar(
                text(
                    "SELECT submit_public_evidence("
                    ":token, :description, :reference, NULL)"
                ),
                {
                    "token": submission_token_hash(raw_token),
                    "description": "Hash is not a token",
                    "reference": "https://example.test/hash-replay",
                },
            )
            accepted = connection.scalar(
                text(
                    "SELECT submit_public_evidence("
                    ":token, :description, :reference, :submitter_name)"
                ),
                {
                    "token": raw_token,
                    "description": "Synthetic public receipt",
                    "reference": "https://example.test/postgres",
                    "submitter_name": "Synthetic Submitter",
                },
            )
            rejected = connection.scalar(
                text(
                    "SELECT submit_public_evidence("
                    ":token, :description, :reference, NULL)"
                ),
                {
                    "token": raw_token,
                    "description": "Second synthetic receipt",
                    "reference": "https://example.test/postgres-2",
                },
            )
            assert hash_replay is None
            assert accepted is not None
            assert rejected is None
        with engine.connect() as connection:
            evidence_count = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM evidence "
                    "WHERE requirement_id = :requirement_id "
                    "AND identity_verified = false"
                ),
                {"requirement_id": seeded["requirement_id"]},
            )
            other_count = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM evidence "
                    "WHERE requirement_id = :requirement_id"
                ),
                {"requirement_id": seeded["other_requirement_id"]},
            )
            status = connection.scalar(
                text("SELECT status FROM requirements WHERE id = :requirement_id"),
                {"requirement_id": seeded["requirement_id"]},
            )
            audit = connection.scalar(
                text(
                    "SELECT after_state::text || COALESCE(actor_reference, '') "
                    "|| COALESCE(correlation_id, '') "
                    "FROM audit_events WHERE event_type = 'evidence.added'"
                )
            )
            stored_hash = connection.scalar(
                text("SELECT token_hash FROM submission_links")
            )
            evidence_text = connection.scalar(
                text(
                    """
                    SELECT COALESCE(description, '')
                        || COALESCE(reference, '')
                        || COALESCE(submitter_name, '')
                    FROM evidence
                    WHERE requirement_id = :requirement_id
                    """
                ),
                {"requirement_id": seeded["requirement_id"]},
            )
        assert evidence_count == 1
        assert other_count == 0
        assert status == "not_started"
        assert raw_token not in audit
        assert stored_hash not in audit
        assert "https://example.test/postgres" not in audit
        assert "submission-link:" in audit
        assert stored_hash == submission_token_hash(raw_token)
        assert raw_token not in stored_hash
        assert raw_token not in evidence_text


@pytest.mark.postgres
def test_unusable_links_leave_no_evidence() -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        valid = generate_submission_token()
        expired = generate_submission_token()
        revoked = generate_submission_token()
        completed = generate_submission_token()
        valid_seed = _seed_postgres_link(engine, valid, max_uses=1)
        expired_seed = _seed_postgres_link(
            engine,
            expired,
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        revoked_seed = _seed_postgres_link(engine, revoked, revoked=True)
        completed_seed = _seed_postgres_link(
            engine,
            completed,
            workflow_status="completed",
        )
        with engine.begin() as connection:
            _enter_public(connection)
            with pytest.raises(SQLAlchemyError) as caught:
                with connection.begin_nested():
                    _submit(connection, valid, reference="http://example.test/no")
            assert valid not in str(caught.value)
            with pytest.raises(SQLAlchemyError) as credentialed:
                with connection.begin_nested():
                    _submit(
                        connection,
                        valid,
                        reference="https://user:secret@example.test/file",
                    )
            assert "secret" not in str(credentialed.value)
            assert _submit(connection, expired) is None
            assert _submit(connection, revoked) is None
            assert _submit(connection, completed) is None
            assert _submit(connection, "0" * 64) is None
            assert _submit(connection, valid, present_stored_hash=True) is None
            completed_context = connection.execute(
                text(
                    "SELECT requirement_title "
                    "FROM public_submission_context(:token)"
                ),
                {"token": completed},
            ).all()
            assert completed_context == []
            assert _submit(connection, valid) is not None
            assert _submit(connection, valid) is None
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM evidence "
                    "WHERE requirement_id = :requirement_id"
                ),
                {"requirement_id": valid_seed["requirement_id"]},
            ) == 1
            for seeded in (expired_seed, revoked_seed, completed_seed):
                assert connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM evidence "
                        "WHERE requirement_id = :requirement_id"
                    ),
                    {"requirement_id": seeded["requirement_id"]},
                ) == 0
                assert connection.scalar(
                    text(
                        "SELECT use_count FROM submission_links WHERE id = :link_id"
                    ),
                    {"link_id": seeded["link_id"]},
                ) == 0


@pytest.mark.postgres
def test_postgres_use_limit_is_atomic() -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        raw_token = generate_submission_token()
        _seed_postgres_link(engine, raw_token, max_uses=1)
        barrier = threading.Barrier(2)
        results: list[object] = []
        errors: list[BaseException] = []

        def attempt() -> None:
            try:
                with engine.connect() as connection:
                    with connection.begin():
                        _enter_public(connection)
                        barrier.wait(timeout=5)
                        results.append(
                            _submit(
                                connection,
                                raw_token,
                                reference="https://example.test/race",
                            )
                        )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        with engine.connect() as connection:
            evidence_count = connection.scalar(text("SELECT COUNT(*) FROM evidence"))
            use_count = connection.scalar(
                text("SELECT MAX(use_count) FROM submission_links")
            )
    assert errors == []
    assert sorted(result is not None for result in results) == [False, True]
    assert evidence_count == 1
    assert use_count == 1


@pytest.mark.postgres
def test_only_active_members_can_manage_submission_links() -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        raw_token = generate_submission_token()
        seeded = _seed_postgres_link(engine, raw_token)
        outsider = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO organization_memberships (
                        organization_id, user_id, is_active
                    )
                    VALUES
                        (:organization_id, :member, true),
                        (:organization_id, :outsider, false),
                        (:other_organization_id, :outsider, true)
                    """
                ),
                {
                    "organization_id": seeded["organization_id"],
                    "other_organization_id": seeded["other_organization_id"],
                    "member": seeded["user_id"],
                    "outsider": outsider,
                },
            )
        request_login = "linkreq_" + uuid4().hex[:12]
        request_engine = _request_engine(engine, url, request_login)
        try:
            repository = SubmissionRepository(create_session_factory(request_engine))
            scope = {
                "organization_id": seeded["organization_id"],
                "workflow_id": seeded["workflow_id"],
                "requirement_id": seeded["requirement_id"],
            }
            listed = repository.list_links(**scope, user_id=seeded["user_id"])
            hidden = repository.list_links(**scope, user_id=outsider)
            with pytest.raises(PersistenceError):
                repository.create_link(
                    **scope,
                    token_hash=submission_token_hash(generate_submission_token()),
                    created_by_user_id=outsider,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    max_uses=1,
                    user_id=outsider,
                )
            with pytest.raises(PersistenceError):
                repository.create_link(
                    organization_id=seeded["other_organization_id"],
                    workflow_id=seeded["workflow_id"],
                    requirement_id=seeded["requirement_id"],
                    token_hash=submission_token_hash(generate_submission_token()),
                    created_by_user_id=outsider,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    max_uses=1,
                    user_id=outsider,
                )
            issued_token = generate_submission_token()
            issued = repository.create_link(
                **scope,
                token_hash=submission_token_hash(issued_token),
                created_by_user_id=seeded["user_id"],
                expires_at=datetime.now(UTC) + timedelta(days=1),
                max_uses=1,
                user_id=seeded["user_id"],
            )
            revoked = repository.revoke_link(
                **scope,
                link_id=seeded["link_id"],
                user_id=seeded["user_id"],
            )
            outsider_revoke = repository.revoke_link(
                **scope,
                link_id=issued.id,
                user_id=outsider,
            )
        finally:
            request_engine.dispose()
            _drop_request_login(engine, request_login)
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            connection.execute(
                text("SELECT set_config('app.user_id', :user_id, true)"),
                {"user_id": str(seeded["user_id"])},
            )
            connection.execute(
                text("SELECT set_config('app.organization_id', :organization_id, true)"),
                {"organization_id": str(seeded["organization_id"])},
            )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text("DELETE FROM submission_links WHERE id = :link_id"),
                        {"link_id": issued.id},
                    )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "UPDATE submission_links SET use_count = 0 "
                            "WHERE id = :link_id"
                        ),
                        {"link_id": issued.id},
                    )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text("SELECT token_hash FROM submission_links")
                    )
            with pytest.raises(SQLAlchemyError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "UPDATE submission_links SET revoked_at = NULL "
                            "WHERE id = :link_id"
                        ),
                        {"link_id": seeded["link_id"]},
                    )
            visible_ids = set(
                connection.scalars(text("SELECT id FROM submission_links")).all()
            )
            _enter_public(connection)
            rejected = _submit(connection, raw_token)
            hash_replay = _submit(connection, issued_token, present_stored_hash=True)
            raw_submission = _submit(connection, issued_token)
        with engine.connect() as connection:
            stored_hash = connection.scalar(
                text("SELECT token_hash FROM submission_links WHERE id = :link_id"),
                {"link_id": issued.id},
            )
            still_revoked = connection.scalar(
                text(
                    "SELECT revoked_at FROM submission_links WHERE id = :link_id"
                ),
                {"link_id": seeded["link_id"]},
            )
        assert [item.id for item in listed] == [seeded["link_id"]]
        assert hidden == []
        assert revoked is not None and revoked.revoked_at is not None
        assert still_revoked is not None
        assert outsider_revoke is None
        assert seeded["link_id"] in visible_ids
        assert issued.id in visible_ids
        assert rejected is None
        assert hash_replay is None
        assert raw_submission is not None
        assert stored_hash == submission_token_hash(issued_token)
        assert issued_token not in stored_hash
        assert raw_token not in stored_hash


@pytest.mark.postgres
def test_cancellation_blocks_an_in_flight_submission() -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        raw_token = generate_submission_token()
        seeded = _seed_postgres_link(engine, raw_token)
        started = threading.Event()
        results: list[object] = []
        errors: list[BaseException] = []
        worker_pids: list[int] = []

        def attempt() -> None:
            try:
                with engine.connect() as connection:
                    with connection.begin():
                        _enter_public(connection)
                        worker_pids.append(
                            connection.scalar(text("SELECT pg_backend_pid()"))
                        )
                        started.set()
                        results.append(
                            _submit(
                                connection,
                                raw_token,
                                reference="https://example.test/cancelled",
                            )
                        )
            except BaseException as exc:
                errors.append(exc)

        with engine.connect() as holder:
            transaction = holder.begin()
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            holder.execute(
                text("SELECT id FROM requirements WHERE id = :requirement_id FOR UPDATE"),
                {"requirement_id": seeded["requirement_id"]},
            )
            worker = threading.Thread(target=attempt)
            worker.start()
            assert started.wait(timeout=5)
            worker_pid = worker_pids[0]
            deadline = time.monotonic() + 5
            blocked = False
            while time.monotonic() < deadline:
                blocked = bool(
                    holder.scalar(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                FROM pg_stat_activity AS activity
                                WHERE activity.pid = :worker_pid
                                  AND activity.wait_event_type = 'Lock'
                                  AND :holder_pid = ANY(
                                      pg_blocking_pids(activity.pid)
                                  )
                            )
                            """
                        ),
                        {
                            "worker_pid": worker_pid,
                            "holder_pid": holder_pid,
                        },
                    )
                )
                if blocked:
                    break
                time.sleep(0.05)
            if blocked:
                holder.execute(
                    text(
                        "UPDATE requirements SET status = 'cancelled' "
                        "WHERE id = :requirement_id"
                    ),
                    {"requirement_id": seeded["requirement_id"]},
                )
            transaction.commit()
        worker.join(timeout=10)
        assert not worker.is_alive()
        with engine.connect() as connection:
            evidence_count = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM evidence "
                    "WHERE requirement_id = :requirement_id"
                ),
                {"requirement_id": seeded["requirement_id"]},
            )
            status = connection.scalar(
                text("SELECT status FROM requirements WHERE id = :requirement_id"),
                {"requirement_id": seeded["requirement_id"]},
            )
    assert blocked
    assert errors == []
    assert results == [None]
    assert evidence_count == 0
    assert status == "cancelled"


@pytest.mark.postgres
def test_cleanup_claim_blocks_a_concurrent_reservation() -> None:
    url = explicit_test_database_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    with isolated_postgres_engine(url) as engine:
        raw_token = generate_submission_token()
        seeded = _seed_postgres_link(engine, raw_token, max_uses=4)
        organization_id = str(seeded["organization_id"])
        referenced_evidence_id = uuid4()
        referenced_key = build_storage_key(
            organization_id, str(referenced_evidence_id), "aa" * 16
        )
        held_evidence_id = uuid4()
        held_key = build_storage_key(
            organization_id, str(held_evidence_id), "bb" * 16
        )
        ready_evidence_id = uuid4()
        ready_key = build_storage_key(
            organization_id, str(ready_evidence_id), "cc" * 16
        )
        protected_evidence_id = uuid4()
        protected_key = build_storage_key(
            organization_id, str(protected_evidence_id), "dd" * 16
        )
        active_evidence_id = uuid4()
        active_key = build_storage_key(
            organization_id, str(active_evidence_id), "ee" * 16
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
                {
                    "organization_id": seeded["organization_id"],
                    "user_id": seeded["user_id"],
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
                        NULL, :submitted_at, NULL, 'submitted', NULL, false
                    )
                    """
                ),
                {
                    "id": referenced_evidence_id,
                    "requirement_id": seeded["requirement_id"],
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
                        'notes.pdf', 'application/pdf', 8,
                        :checksum, :uploaded_at, 'unscanned'
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "evidence_id": referenced_evidence_id,
                    "organization_id": seeded["organization_id"],
                    "storage_key": referenced_key,
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
                    "storage_key": referenced_key,
                    "organization_id": seeded["organization_id"],
                    "recorded_at": now - timedelta(days=3),
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO evidence_file_reservations (
                        storage_key, evidence_id, organization_id,
                        reserved_at, expires_at
                    )
                    VALUES (
                        :storage_key, :evidence_id, :organization_id,
                        :reserved_at, :expires_at
                    )
                    """
                ),
                {
                    "storage_key": active_key,
                    "evidence_id": active_evidence_id,
                    "organization_id": seeded["organization_id"],
                    "reserved_at": now,
                    "expires_at": now + timedelta(hours=1),
                },
            )
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text("SET LOCAL ROLE commitmentops_maintenance"))
            with pytest.raises(SQLAlchemyError, match="invalid submission"):
                connection.execute(
                    text("SELECT * FROM claim_evidence_storage_key(60)")
                )
            transaction.rollback()
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_maintenance"))
            missed = connection.execute(
                text(
                    "SELECT claimed_storage_key FROM "
                    "claim_evidence_storage_key(86400)"
                )
            ).one_or_none()
        assert missed is None
        with engine.begin() as connection:
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
                    "storage_key": held_key,
                    "organization_id": seeded["organization_id"],
                    "recorded_at": now - timedelta(days=2),
                },
            )
        started = threading.Event()
        errors: list[BaseException] = []
        worker_pids: list[int] = []

        def attempt() -> None:
            try:
                with engine.connect() as connection:
                    with connection.begin():
                        _enter_public(connection)
                        worker_pids.append(
                            connection.scalar(text("SELECT pg_backend_pid()"))
                        )
                        started.set()
                        connection.execute(
                            text(
                                """
                                SELECT reserve_evidence_object(
                                    :token, :evidence_id, :storage_key
                                )
                                """
                            ),
                            {
                                "token": raw_token,
                                "evidence_id": held_evidence_id,
                                "storage_key": held_key,
                            },
                        )
            except BaseException as exc:
                errors.append(exc)

        removed: list[str] = []
        with engine.connect() as holder:
            transaction = holder.begin()
            holder.execute(text("SET LOCAL ROLE commitmentops_maintenance"))
            holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
            claimed = holder.execute(
                text(
                    "SELECT claimed_storage_key, claimed_token "
                    "FROM claim_evidence_storage_key(86400)"
                )
            ).one()
            holder.execute(text("RESET ROLE"))
            assert claimed.claimed_storage_key == held_key
            original_token = claimed.claimed_token
            worker = threading.Thread(target=attempt)
            worker.start()
            assert started.wait(timeout=5)
            deadline = time.monotonic() + 5
            blocked = False
            while time.monotonic() < deadline:
                blocked = bool(
                    holder.scalar(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                FROM pg_stat_activity AS activity
                                WHERE activity.pid = :worker_pid
                                  AND activity.wait_event_type = 'Lock'
                                  AND :holder_pid = ANY(
                                      pg_blocking_pids(activity.pid)
                                  )
                            )
                            """
                        ),
                        {
                            "worker_pid": worker_pids[0],
                            "holder_pid": holder_pid,
                        },
                    )
                )
                if blocked:
                    break
                time.sleep(0.05)
            assert removed == []
            transaction.commit()
        worker.join(timeout=10)
        assert blocked
        assert not worker.is_alive()
        assert len(errors) == 1
        assert "invalid submission" in str(errors[0]).lower()
        with engine.connect() as connection:
            reservation_count = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM evidence_file_reservations "
                    "WHERE storage_key = :storage_key"
                ),
                {"storage_key": held_key},
            )
            claimed_at = connection.scalar(
                text(
                    "SELECT claimed_at FROM evidence_storage_cleanup "
                    "WHERE storage_key = :storage_key"
                ),
                {"storage_key": held_key},
            )
            use_count = connection.scalar(
                text("SELECT use_count FROM submission_links")
            )
        assert reservation_count == 0
        assert claimed_at is not None
        assert use_count == 0
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_maintenance"))
            stolen = connection.execute(
                text(
                    "SELECT claimed_storage_key FROM "
                    "claim_evidence_storage_key(86400)"
                )
            ).one_or_none()
        assert stolen is None
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE evidence_storage_cleanup
                       SET claimed_at = clock_timestamp() - interval '6 minutes'
                     WHERE storage_key = :storage_key
                    """
                ),
                {"storage_key": held_key},
            )
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_maintenance"))
            adopted = connection.execute(
                text(
                    "SELECT claimed_storage_key, claimed_token "
                    "FROM claim_evidence_storage_key(86400)"
                )
            ).one()
        assert adopted.claimed_storage_key == held_key
        assert adopted.claimed_token != original_token
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text("SET LOCAL ROLE commitmentops_maintenance"))
            with pytest.raises(SQLAlchemyError, match="invalid submission"):
                connection.execute(
                    text(
                        """
                        SELECT complete_evidence_storage_cleanup(
                            :storage_key, :claim_token
                        )
                        """
                    ),
                    {
                        "storage_key": held_key,
                        "claim_token": original_token,
                    },
                )
            transaction.rollback()
        with engine.connect() as connection:
            still_claimed = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM evidence_storage_cleanup "
                    "WHERE storage_key = :storage_key"
                ),
                {"storage_key": held_key},
            )
        assert still_claimed == 1
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_maintenance"))
            connection.execute(
                text(
                    """
                    SELECT complete_evidence_storage_cleanup(
                        :storage_key, :claim_token
                    )
                    """
                ),
                {
                    "storage_key": held_key,
                    "claim_token": adopted.claimed_token,
                },
            )
        storage = FakeObjectStorage()
        storage.put_new(ready_key, b"%PDF-1.4\n", "application/pdf")
        with engine.begin() as connection:
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
                    "storage_key": ready_key,
                    "organization_id": seeded["organization_id"],
                    "recorded_at": now - timedelta(days=4),
                },
            )

        def delete_after_reserve_fails(storage_key: str) -> None:
            with engine.connect() as connection:
                with connection.begin():
                    _enter_public(connection)
                    with pytest.raises(SQLAlchemyError, match="invalid submission"):
                        connection.execute(
                            text(
                                """
                                SELECT reserve_evidence_object(
                                    :token, :evidence_id, :storage_key
                                )
                                """
                            ),
                            {
                                "token": raw_token,
                                "evidence_id": ready_evidence_id,
                                "storage_key": storage_key,
                            },
                        )
            removed.append(storage_key)
            storage._objects.pop(storage_key, None)

        storage.delete = delete_after_reserve_fails
        repository = SubmissionRepository(
            create_session_factory(engine),
            storage,
        )
        deleted = repository.cleanup_unreferenced_objects()
        assert deleted == 1
        assert removed == [ready_key]
        assert ready_key not in storage._objects
        assert held_key not in removed
        protected_token = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO evidence_storage_cleanup (
                        storage_key, organization_id, recorded_at,
                        claimed_at, claim_token
                    )
                    VALUES (
                        :storage_key, :organization_id, :recorded_at,
                        :claimed_at, :claim_token
                    )
                    """
                ),
                {
                    "storage_key": protected_key,
                    "organization_id": seeded["organization_id"],
                    "recorded_at": now - timedelta(days=3),
                    "claimed_at": now,
                    "claim_token": protected_token,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO evidence_file_reservations (
                        storage_key, evidence_id, organization_id,
                        reserved_at, expires_at
                    )
                    VALUES (
                        :storage_key, :evidence_id, :organization_id,
                        :reserved_at, :expires_at
                    )
                    """
                ),
                {
                    "storage_key": protected_key,
                    "evidence_id": protected_evidence_id,
                    "organization_id": seeded["organization_id"],
                    "reserved_at": now,
                    "expires_at": now + timedelta(hours=1),
                },
            )
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text("SET LOCAL ROLE commitmentops_maintenance"))
            with pytest.raises(SQLAlchemyError, match="invalid submission"):
                connection.execute(
                    text(
                        """
                        SELECT complete_evidence_storage_cleanup(
                            :storage_key, :claim_token
                        )
                        """
                    ),
                    {
                        "storage_key": protected_key,
                        "claim_token": protected_token,
                    },
                )
            transaction.rollback()
        with engine.connect() as connection:
            protected_cleanup = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM evidence_storage_cleanup "
                    "WHERE storage_key = :storage_key"
                ),
                {"storage_key": protected_key},
            )
            protected_reservation = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM evidence_file_reservations "
                    "WHERE storage_key = :storage_key"
                ),
                {"storage_key": protected_key},
            )
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            connection.execute(
                text("SELECT set_config('app.user_id', :user_id, true)"),
                {"user_id": str(seeded["user_id"])},
            )
            connection.execute(
                text("SELECT set_config('app.organization_id', :organization_id, true)"),
                {"organization_id": str(seeded["organization_id"])},
            )
            filenames = connection.execute(
                text("SELECT original_filename FROM evidence_files")
            ).all()
            with pytest.raises(SQLAlchemyError, match="permission denied"):
                connection.execute(text("SELECT storage_key FROM evidence_files"))
        assert protected_cleanup == 1
        assert protected_reservation == 1
        assert filenames == [("notes.pdf",)]
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            with pytest.raises(SQLAlchemyError, match="permission denied"):
                connection.execute(
                    text("SELECT * FROM claim_evidence_storage_key(86400)")
                )
            transaction.rollback()


def _request_engine(engine, url: str, login: str):
    if not login.startswith("linkreq_") or not login.replace("_", "").isalnum():
        raise AssertionError("refusing an unexpected request login")
    schema = require_test_target(engine).schema
    quoted = quoted_isolated_schema(schema)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE ROLE {login} LOGIN NOSUPERUSER NOBYPASSRLS "
                "NOCREATEDB NOCREATEROLE NOINHERIT"
            )
        )
        connection.execute(text(f"GRANT {APP_ROLE} TO {login}"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA {quoted} TO {login}"))
    parsed = make_url(url)
    request_url = URL.create(
        drivername=parsed.drivername,
        username=login,
        password=None,
        host=parsed.host,
        port=parsed.port,
        database=parsed.database,
    ).render_as_string(hide_password=False)
    try:
        return create_database_engine(
            request_url,
            schema=schema,
            connect_args={"connect_timeout": 5},
        )
    except Exception:
        _drop_request_login(engine, login)
        raise


def _drop_request_login(engine, login: str) -> None:
    if not login.startswith("linkreq_") or not login.replace("_", "").isalnum():
        raise AssertionError("refusing to drop an unexpected login")
    quoted = quoted_isolated_schema(require_test_target(engine).schema)
    with engine.begin() as connection:
        exists = connection.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
            {"role": login},
        )
        if not exists:
            return
        connection.execute(text(f"REVOKE {APP_ROLE} FROM {login}"))
        connection.execute(text(f"REVOKE ALL ON SCHEMA {quoted} FROM {login}"))
        connection.execute(text(f"DROP ROLE {login}"))


def _enter_public(connection) -> None:
    connection.execute(text("SET LOCAL ROLE commitmentops_app"))
    connection.execute(text("SELECT set_config('app.user_id', '', true)"))
    connection.execute(
        text("SELECT set_config('app.organization_id', '', true)")
    )


def _submit(
    connection,
    token: str,
    *,
    reference: str = "https://example.test/receipt",
    present_stored_hash: bool = False,
):
    presented = submission_token_hash(token) if present_stored_hash else token
    return connection.scalar(
        text(
            """
            SELECT submit_public_evidence(
                :token, :description, :reference, NULL
            )
            """
        ),
        {
            "token": presented,
            "description": "Synthetic receipt",
            "reference": reference,
        },
    )


def _seed_postgres_link(
    engine,
    raw_token: str,
    *,
    max_uses: int = 1,
    workflow_status: str = "active",
    expires_at: datetime | None = None,
    revoked: bool = False,
) -> dict:
    organization_id = uuid4()
    other_organization_id = uuid4()
    user_id = uuid4()
    workflow_id = uuid4()
    other_workflow_id = uuid4()
    requirement_id = uuid4()
    other_requirement_id = uuid4()
    link_id = uuid4()
    now = datetime.now(UTC)
    expires_at = expires_at or now + timedelta(days=1)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO organizations (id, name) VALUES (:id, :name)"),
            [
                {"id": organization_id, "name": "Synthetic Organization"},
                {"id": other_organization_id, "name": "Other Synthetic Organization"},
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO workflows (
                    id, organization_id, name, workflow_type, status
                )
                VALUES (
                    :id, :organization_id, :name, 'general', :status
                )
                """
            ),
            [
                {
                    "id": workflow_id,
                    "organization_id": organization_id,
                    "name": "Synthetic workflow",
                    "status": workflow_status,
                },
                {
                    "id": other_workflow_id,
                    "organization_id": other_organization_id,
                    "name": "Other workflow",
                    "status": "active",
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO people (id, name, email)
                VALUES (:id, 'Synthetic Owner', 'owner@example.org')
                """
            ),
            {"id": user_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO requirements (
                    id, workflow_id, title, description, owner_id,
                    evidence_expectation, provenance, status, review_status
                )
                VALUES (
                    :id, :workflow_id, :title, 'Synthetic',
                    :owner_id, 'A synthetic record', 'user_provided',
                    'not_started', 'approved'
                )
                """
            ),
            [
                {
                    "id": requirement_id,
                    "workflow_id": workflow_id,
                    "title": "Synthetic requirement",
                    "owner_id": user_id,
                },
                {
                    "id": other_requirement_id,
                    "workflow_id": other_workflow_id,
                    "title": "Other requirement",
                    "owner_id": user_id,
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO submission_links (
                    id, organization_id, workflow_id, requirement_id, token_hash,
                    created_at, expires_at, revoked_at, created_by_user_id,
                    max_uses, use_count
                )
                VALUES (
                    :id, :organization_id, :workflow_id, :requirement_id, :token_hash,
                    :created_at, :expires_at, :revoked_at, :created_by_user_id,
                    :max_uses, 0
                )
                """
            ),
            {
                "id": link_id,
                "organization_id": organization_id,
                "workflow_id": workflow_id,
                "requirement_id": requirement_id,
                "token_hash": submission_token_hash(raw_token),
                "created_at": now,
                "expires_at": expires_at,
                "revoked_at": now if revoked else None,
                "created_by_user_id": user_id,
                "max_uses": max_uses,
            },
        )
    return {
        "organization_id": organization_id,
        "other_organization_id": other_organization_id,
        "user_id": user_id,
        "workflow_id": workflow_id,
        "requirement_id": requirement_id,
        "other_requirement_id": other_requirement_id,
        "link_id": link_id,
    }


def _assert_function_boundary(connection) -> None:
    functions = connection.execute(
        text(
            """
            SELECT
                proname,
                prosecdef,
                proconfig,
                prosrc,
                pg_get_function_identity_arguments(pg_proc.oid) AS arguments,
                pg_get_userbyid(proowner) AS owner
            FROM pg_proc
            JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
            WHERE pg_namespace.nspname = current_schema()
              AND proname IN (
                    'public_submission_context',
                    'submit_public_evidence'
              )
            ORDER BY proname
            """
        )
    ).all()
    assert [row.proname for row in functions] == [
        "public_submission_context",
        "submit_public_evidence",
    ]
    assert [row.arguments for row in functions] == [
        "p_token text",
        "p_token text, p_description text, p_reference text, p_submitter_name text",
    ]
    for row in functions:
        assert row.prosecdef is True
        assert row.proconfig == ["search_path=pg_catalog"]
        assert row.owner == "commitmentops_submission"
        assert "EXECUTE" not in row.prosrc.upper()
        assert "format(" not in row.prosrc
        assert "digest(" in row.prosrc
        lowered = row.prosrc.lower()
        assert "current_setting(" not in lowered
        if "json_build_object" in row.prosrc:
            assert "p_token" not in row.prosrc.split("json_build_object", 1)[1]
        assert '"commitmentops_crypto".digest(' in row.prosrc
    file_functions = connection.execute(
        text(
            """
            SELECT
                proname,
                prosecdef,
                proconfig,
                prosrc,
                pg_get_function_identity_arguments(pg_proc.oid) AS arguments,
                pg_get_userbyid(proowner) AS owner
            FROM pg_proc
            JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
            WHERE pg_namespace.nspname = current_schema()
              AND proname IN (
                    'submission_target_organization',
                    'reserve_evidence_object',
                    'submit_public_evidence_with_file',
                    'record_evidence_storage_failure',
                    'release_evidence_object_reservation',
                    'claim_evidence_storage_key',
                    'complete_evidence_storage_cleanup'
              )
            ORDER BY proname
            """
        )
    ).all()
    assert [row.proname for row in file_functions] == [
        "claim_evidence_storage_key",
        "complete_evidence_storage_cleanup",
        "record_evidence_storage_failure",
        "release_evidence_object_reservation",
        "reserve_evidence_object",
        "submission_target_organization",
        "submit_public_evidence_with_file",
    ]
    for row in file_functions:
        assert row.prosecdef is True
        assert row.proconfig == ["search_path=pg_catalog"]
        assert row.owner == "commitmentops_submission"
        assert "EXECUTE" not in row.prosrc.upper()
        assert "format(" not in row.prosrc
        assert "current_setting(" not in row.prosrc.lower()
        if row.proname != "submission_target_organization":
            assert "pg_advisory_xact_lock" in row.prosrc
        if "p_token" in row.arguments:
            assert '"commitmentops_crypto".digest(' in row.prosrc
        if "json_build_object" in row.prosrc:
            after = row.prosrc.split("json_build_object", 1)[1]
            assert "p_token" not in after
            assert "p_filename" not in after
            assert "p_storage_key" not in after
    executable = set(
        connection.scalars(
            text(
                """
                SELECT proname || '('
                    || pg_get_function_identity_arguments(pg_proc.oid) || ')'
                FROM pg_proc
                JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
                WHERE pg_namespace.nspname = current_schema()
                  AND has_function_privilege(
                        'commitmentops_app', pg_proc.oid, 'EXECUTE'
                  )
                """
            )
        ).all()
    )
    assert executable == {
        "app_current_organization_id()",
        "app_current_user_id()",
        "app_is_active_member(target_organization uuid)",
        "public_submission_context(p_token text)",
        "submission_links_keep_revocation()",
        "submit_public_evidence(p_token text, p_description text, p_reference text, p_submitter_name text)",
        "submission_target_organization(p_token text)",
        "reserve_evidence_object(p_token text, p_evidence_id uuid, p_storage_key text)",
        "submit_public_evidence_with_file(p_token text, p_description text, p_reference text, p_submitter_name text, p_evidence_id uuid, p_storage_key text, p_filename text, p_media_type text, p_byte_size integer, p_checksum text)",
        "record_evidence_storage_failure(p_storage_key text)",
        "release_evidence_object_reservation(p_storage_key text)",
    }
    for signature in (
        "public_submission_context(text)",
        "submission_links_keep_revocation()",
        "submit_public_evidence(text, text, text, text)",
        "submission_target_organization(text)",
        "reserve_evidence_object(text, uuid, text)",
        "submit_public_evidence_with_file(text, text, text, text, uuid, text, text, text, integer, text)",
        "record_evidence_storage_failure(text)",
        "release_evidence_object_reservation(text)",
    ):
        assert connection.scalar(
            text(
                "SELECT has_function_privilege('public', :signature, 'EXECUTE')"
            ),
            {"signature": signature},
        ) is False
        assert connection.scalar(
            text(
                """
                SELECT has_function_privilege(
                    'commitmentops_app', :signature, 'EXECUTE'
                )
                """
            ),
            {"signature": signature},
        ) is True
        assert connection.scalar(
            text(
                """
                SELECT has_function_privilege(
                    'commitmentops_maintenance', :signature, 'EXECUTE'
                )
                """
            ),
            {"signature": signature},
        ) is False
    for signature in (
        "claim_evidence_storage_key(integer)",
        "complete_evidence_storage_cleanup(text, uuid)",
    ):
        assert connection.scalar(
            text(
                "SELECT has_function_privilege('public', :signature, 'EXECUTE')"
            ),
            {"signature": signature},
        ) is False
        assert connection.scalar(
            text(
                """
                SELECT has_function_privilege(
                    'commitmentops_app', :signature, 'EXECUTE'
                )
                """
            ),
            {"signature": signature},
        ) is False
        assert connection.scalar(
            text(
                """
                SELECT has_function_privilege(
                    'commitmentops_maintenance', :signature, 'EXECUTE'
                )
                """
            ),
            {"signature": signature},
        ) is True
    granted_owner = connection.scalar(
        text(
            """
            SELECT COUNT(*)
            FROM pg_auth_members AS membership
            JOIN pg_roles AS owned ON owned.oid = membership.roleid
            WHERE owned.rolname = 'commitmentops_submission'
            """
        )
    )
    owner = connection.execute(
        text(
            """
            SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
            FROM pg_roles
            WHERE rolname = 'commitmentops_submission'
            """
        )
    ).one()
    forced = connection.execute(
        text(
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_class
            JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            WHERE pg_namespace.nspname = current_schema()
              AND pg_class.relname = 'submission_links'
            """
        )
    ).one()
    app_privileges = set(
        connection.scalars(
            text(
                """
                SELECT privilege_type
                FROM information_schema.table_privileges
                WHERE table_schema = current_schema()
                  AND table_name = 'submission_links'
                  AND grantee = 'commitmentops_app'
                """
            )
        ).all()
    )
    app_update_columns = set(
        connection.scalars(
            text(
                """
                SELECT column_name
                FROM information_schema.column_privileges
                WHERE table_schema = current_schema()
                  AND table_name = 'submission_links'
                  AND grantee = 'commitmentops_app'
                  AND privilege_type = 'UPDATE'
                """
            )
        ).all()
    )
    submission_update_columns = set(
        connection.scalars(
            text(
                """
                SELECT column_name
                FROM information_schema.column_privileges
                WHERE table_schema = current_schema()
                  AND table_name = 'submission_links'
                  AND grantee = 'commitmentops_submission'
                  AND privilege_type = 'UPDATE'
                """
            )
        ).all()
    )
    app_is_submission_member = connection.scalar(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_auth_members
                JOIN pg_roles AS member_role
                  ON member_role.oid = pg_auth_members.member
                JOIN pg_roles AS granted_role
                  ON granted_role.oid = pg_auth_members.roleid
                WHERE member_role.rolname = 'commitmentops_app'
                  AND granted_role.rolname = 'commitmentops_submission'
            )
            """
        )
    )
    app_select_columns = set(
        connection.scalars(
            text(
                """
                SELECT column_name
                FROM information_schema.column_privileges
                WHERE table_schema = current_schema()
                  AND table_name = 'submission_links'
                  AND grantee = 'commitmentops_app'
                  AND privilege_type = 'SELECT'
                """
            )
        ).all()
    )
    assert granted_owner == 0
    assert app_is_submission_member is False
    assert tuple(owner) == (False, False, True, False, False)
    assert tuple(forced) == (True, True)
    assert app_privileges == {"INSERT"}
    assert app_select_columns == {
        "id",
        "organization_id",
        "workflow_id",
        "requirement_id",
        "created_at",
        "expires_at",
        "revoked_at",
        "created_by_user_id",
        "max_uses",
        "use_count",
        "last_used_at",
    }
    assert app_update_columns == {"revoked_at"}
    assert submission_update_columns == {"use_count", "last_used_at"}
    for table, untouched_column in (
        ("workflows", "name"),
        ("requirements", "title"),
    ):
        update_columns = set(
            connection.scalars(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.column_privileges
                    WHERE table_schema = current_schema()
                      AND table_name = :table_name
                      AND grantee = 'commitmentops_submission'
                      AND privilege_type = 'UPDATE'
                    """
                ),
                {"table_name": table},
            ).all()
        )
        assert update_columns == {"status"}
        assert connection.scalar(
            text(
                """
                SELECT has_column_privilege(
                    'commitmentops_submission', :table_name, 'status', 'UPDATE'
                )
                """
            ),
            {"table_name": table},
        ) is True
        assert connection.scalar(
            text(
                """
                SELECT has_column_privilege(
                    'commitmentops_submission',
                    :table_name,
                    :column_name,
                    'UPDATE'
                )
                """
            ),
            {"table_name": table, "column_name": untouched_column},
        ) is False
        for privilege in ("UPDATE", "INSERT", "DELETE"):
            assert connection.scalar(
                text(
                    """
                    SELECT has_table_privilege(
                        'commitmentops_submission', :table_name, :privilege
                    )
                    """
                ),
                {"table_name": table, "privilege": privilege},
            ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_table_privilege(
                'commitmentops_app', 'submission_links', 'SELECT'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_table_privilege(
                'commitmentops_app', 'submission_links', 'UPDATE'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_table_privilege(
                'commitmentops_app', 'submission_links', 'DELETE'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_column_privilege(
                'commitmentops_app',
                'submission_links',
                'token_hash',
                'UPDATE'
            )
            """
        )
    ) is False
    app_hash_select = connection.scalar(
        text(
            """
            SELECT has_column_privilege(
                'commitmentops_app',
                'submission_links',
                'token_hash',
                'SELECT'
            )
            """
        )
    )
    owner_hash_select = connection.scalar(
        text(
            """
            SELECT has_column_privilege(
                'commitmentops_submission',
                'submission_links',
                'token_hash',
                'SELECT'
            )
            """
        )
    )
    guard = connection.execute(
        text(
            """
            SELECT prosecdef, proconfig, pg_get_userbyid(proowner) AS owner
            FROM pg_proc
            JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
            WHERE pg_namespace.nspname = current_schema()
              AND proname = 'submission_links_keep_revocation'
            """
        )
    ).one()
    assert app_hash_select is False
    assert owner_hash_select is True
    assert guard.prosecdef is False
    assert guard.proconfig == ["search_path=pg_catalog"]
    assert guard.owner != "commitmentops_app"
    assert guard.owner != "commitmentops_submission"
    crypto_schema = connection.scalar(
        text(
            """
            SELECT namespace.nspname
            FROM pg_extension AS extension
            JOIN pg_namespace AS namespace
              ON namespace.oid = extension.extnamespace
            WHERE extension.extname = 'pgcrypto'
            """
        )
    )
    assert crypto_schema == "commitmentops_crypto"
    assert connection.scalar(
        text(
            """
            SELECT has_schema_privilege(
                'commitmentops_app', 'commitmentops_crypto', 'USAGE'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_schema_privilege(
                'commitmentops_submission', 'commitmentops_crypto', 'USAGE'
            )
            """
        )
    ) is True
    for role, signature, allowed in (
        ("commitmentops_app", "commitmentops_crypto.digest(bytea, text)", False),
        ("commitmentops_submission", "commitmentops_crypto.digest(bytea, text)", True),
        ("public", "commitmentops_crypto.digest(bytea, text)", False),
        ("commitmentops_app", "commitmentops_crypto.digest(text, text)", False),
        ("commitmentops_submission", "commitmentops_crypto.digest(text, text)", False),
        ("commitmentops_app", "commitmentops_crypto.crypt(text, text)", False),
        ("commitmentops_submission", "commitmentops_crypto.crypt(text, text)", False),
    ):
        assert connection.scalar(
            text(
                """
                SELECT has_function_privilege(:role, :signature, 'EXECUTE')
                """
            ),
            {"role": role, "signature": signature},
        ) is allowed
    assert connection.scalar(
        text(
            """
            SELECT has_function_privilege(
                'commitmentops_submission',
                'submission_links_keep_revocation()',
                'EXECUTE'
            )
            """
        )
    ) is True
    maintenance = connection.execute(
        text(
            """
            SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
            FROM pg_roles
            WHERE rolname = 'commitmentops_maintenance'
            """
        )
    ).one()
    assert tuple(maintenance) == (False, False, False, False, False)
    assert connection.scalar(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_auth_members
                JOIN pg_roles AS member_role
                  ON member_role.oid = pg_auth_members.member
                JOIN pg_roles AS granted_role
                  ON granted_role.oid = pg_auth_members.roleid
                WHERE member_role.rolname = 'commitmentops_app'
                  AND granted_role.rolname = 'commitmentops_maintenance'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_table_privilege(
                'commitmentops_submission', 'evidence_files', 'DELETE'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_table_privilege(
                'commitmentops_submission', 'evidence_storage_cleanup', 'UPDATE'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_column_privilege(
                'commitmentops_submission',
                'evidence_storage_cleanup',
                'claimed_at',
                'UPDATE'
            )
            """
        )
    ) is True
    assert connection.scalar(
        text(
            """
            SELECT has_column_privilege(
                'commitmentops_submission',
                'evidence_storage_cleanup',
                'claim_token',
                'UPDATE'
            )
            """
        )
    ) is True
    assert connection.scalar(
        text(
            """
            SELECT has_column_privilege(
                'commitmentops_submission',
                'evidence_storage_cleanup',
                'storage_key',
                'UPDATE'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_column_privilege(
                'commitmentops_app',
                'evidence_storage_cleanup',
                'claimed_at',
                'UPDATE'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_table_privilege(
                'commitmentops_app', 'evidence_files', 'DELETE'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_column_privilege(
                'commitmentops_app', 'evidence_files', 'storage_key', 'SELECT'
            )
            """
        )
    ) is False
    assert connection.scalar(
        text(
            """
            SELECT has_column_privilege(
                'commitmentops_app',
                'evidence_files',
                'original_filename',
                'SELECT'
            )
            """
        )
    ) is True
    for table_name in (
        "evidence_files",
        "evidence_file_reservations",
        "evidence_storage_cleanup",
    ):
        forced_files = connection.execute(
            text(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class
                JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
                WHERE pg_namespace.nspname = current_schema()
                  AND pg_class.relname = :table_name
                """
            ),
            {"table_name": table_name},
        ).one()
        assert tuple(forced_files) == (True, True)
    assert EvidenceType.LINK.value == "link"
