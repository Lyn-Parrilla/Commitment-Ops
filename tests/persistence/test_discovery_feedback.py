"""Durable review decisions stay separate from learning signals."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.domain import (
    CandidateRevision,
    DocumentType,
    HumanDecision,
    LearningSignal,
    Person,
    Requirement,
    RequirementCandidate,
    RequirementProvenance,
    ReviewStatus,
    SourceDocument,
    SourceLocation,
)
from app.persistence import (
    Base,
    DiscoveryRepository,
    Organization,
    OrganizationAdmin,
    PersistenceError,
    create_database_engine,
    create_session_factory,
)
from app.persistence.database import SessionFactory
from app.persistence.orm import RequirementRecord
from tests.persistence.postgres_support import (
    LEGACY_ORGANIZATION_ID,
    SKIP_REASON,
    explicit_test_database_url,
    isolated_postgres_engine,
)

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000002")
USER_ID = UUID("20000000-0000-0000-0000-000000000001")
OTHER_USER_ID = UUID("20000000-0000-0000-0000-000000000002")


@pytest.fixture
def discovery_repository(tmp_path: Path) -> Iterator[DiscoveryRepository]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'review.db'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    admin = OrganizationAdmin(session_factory)
    for organization_id, name in (
        (ORGANIZATION_ID, "Synthetic Organization"),
        (OTHER_ORGANIZATION_ID, "Other Synthetic Organization"),
    ):
        admin.save(Organization(id=organization_id, name=name))
    yield DiscoveryRepository(session_factory)
    engine.dispose()


def test_decision_and_signal_survive_reload(
    discovery_repository: DiscoveryRepository,
) -> None:
    candidate = _candidate()
    reviewer = _reviewer()
    approval = candidate.approve(reviewer=reviewer, note="Confirmed the clause.")
    discovery_repository.save_candidate(
        candidate,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )

    loaded = discovery_repository.get_candidate(
        candidate.id,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )

    assert loaded is not None
    assert loaded.is_authoritative is False
    assert loaded.review_status == ReviewStatus.APPROVED
    assert loaded.reviewed_by is not None
    assert loaded.reviewed_by.id == reviewer.id
    assert loaded.reviewed_at == candidate.reviewed_at
    assert loaded.source_document_snapshot.filename == "award.pdf"
    assert loaded.source_location is not None
    assert loaded.source_location.page == 4
    assert loaded.source_location.section == "Reporting"
    signal = loaded.learning_signals[0]
    assert signal.id == approval.learning_signal.id
    assert signal.decision_id == approval.learning_signal.decision_id
    assert signal.decision_id != signal.id
    assert signal.human_decision == HumanDecision.APPROVED
    assert signal.reviewer.id == reviewer.id
    assert signal.decided_at == approval.learning_signal.decided_at
    assert signal.original_discovery is not None
    assert signal.original_discovery.title == "Quarterly narrative"
    assert signal.source_document.filename == "award.pdf"
    assert signal.source_location is not None
    assert signal.source_location.page == 4
    assert signal.resulting_requirement is not None
    assert signal.resulting_requirement.requirement_id == approval.requirement.id
    assert (
        signal.resulting_requirement.provenance == RequirementProvenance.AI_INFERRED
    )
    session_factory: SessionFactory = discovery_repository._session_factory
    with session_factory() as session:
        stored_requirements = session.scalar(
            select(func.count()).select_from(RequirementRecord)
        )
    assert stored_requirements == 0


def test_rejection_revision_and_second_decision_keep_history(
    discovery_repository: DiscoveryRepository,
) -> None:
    candidate = _candidate()
    reviewer = _reviewer()
    candidate.reject(reviewer=reviewer, reason="Owner is incomplete.")
    discovery_repository.save_candidate(
        candidate,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )
    candidate.revise(CandidateRevision(title="Quarterly financial narrative"))
    discovery_repository.save_candidate(
        candidate,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )

    pending = discovery_repository.get_candidate(
        candidate.id,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )
    assert pending is not None
    assert pending.review_status == ReviewStatus.PENDING_REVIEW
    assert pending.reviewed_by is None
    assert pending.title == "Quarterly financial narrative"
    assert len(pending.learning_signals) == 1
    assert pending.learning_signals[0].human_decision == HumanDecision.REJECTED
    assert pending.learning_signals[0].original_discovery is not None
    assert pending.learning_signals[0].original_discovery.title == "Quarterly narrative"
    assert pending.is_authoritative is False

    candidate.approve(reviewer=reviewer, note="Corrected and confirmed.")
    discovery_repository.save_candidate(
        candidate,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )
    approved = discovery_repository.get_candidate(
        candidate.id,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )
    assert approved is not None
    assert [signal.human_decision for signal in approved.learning_signals] == [
        HumanDecision.REJECTED,
        HumanDecision.APPROVED,
    ]
    assert approved.learning_signals[0].decision_id != approved.learning_signals[1].decision_id


def test_repeated_save_does_not_duplicate_or_overwrite_feedback(
    discovery_repository: DiscoveryRepository,
) -> None:
    candidate = _candidate()
    candidate.reject(reviewer=_reviewer(), reason="Example, not an obligation.")
    discovery_repository.save_candidate(
        candidate,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )
    discovery_repository.save_candidate(
        candidate,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )
    reloaded = discovery_repository.get_candidate(
        candidate.id,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )
    assert reloaded is not None
    assert len(reloaded.learning_signals) == 1

    changed = reloaded.model_copy(deep=True)
    changed.learning_signals = [
        reloaded.learning_signals[0].model_copy(
            update={"decision_reason": "Different reason"}
        )
    ]
    with pytest.raises(PersistenceError, match="overwrite a recorded review decision"):
        discovery_repository.save_candidate(
            changed,
            organization_id=ORGANIZATION_ID,
            user_id=USER_ID,
        )
    preserved = discovery_repository.get_candidate(
        candidate.id,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )
    assert preserved is not None
    assert preserved.learning_signals[0].decision_reason == "Example, not an obligation."


def test_human_added_feedback_survives_reload(
    discovery_repository: DiscoveryRepository,
) -> None:
    reviewer = _reviewer()
    requirement = Requirement.create(
        title="Missed insurance certificate",
        description="The vendor must file the certificate before work starts.",
        owner=Person.create(name="Alex Rivera", email="alex.rivera@example.org"),
        evidence_expectation="Certificate on file",
        provenance=RequirementProvenance.USER_PROVIDED,
    )
    signal = LearningSignal.record_human_added(
        source_document=_document(),
        source_location=SourceLocation(page=2, heading="Insurance"),
        requirement=requirement,
        reviewer=reviewer,
        reason="The extractor skipped the certificate clause.",
    )
    discovery_repository.save_human_added(
        signal,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )

    loaded = discovery_repository.get_signal(
        signal.id,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )

    assert loaded is not None
    assert loaded.candidate_id is None
    assert loaded.decision_id != loaded.id
    assert loaded.human_decision == HumanDecision.HUMAN_ADDED
    assert loaded.reviewer.id == reviewer.id
    assert loaded.decided_at == signal.decided_at
    assert loaded.source_location is not None
    assert loaded.source_location.heading == "Insurance"
    assert loaded.resulting_requirement is not None
    assert loaded.resulting_requirement.requirement_id == requirement.id
    assert discovery_repository.get_candidate(
        signal.id,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    ) is None


def test_other_organization_cannot_read_review_records(
    discovery_repository: DiscoveryRepository,
) -> None:
    candidate = _candidate()
    candidate.reject(reviewer=_reviewer(), reason="Not our organization.")
    discovery_repository.save_candidate(
        candidate,
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
    )

    assert (
        discovery_repository.get_candidate(
            candidate.id,
            organization_id=OTHER_ORGANIZATION_ID,
            user_id=OTHER_USER_ID,
        )
        is None
    )
    assert (
        discovery_repository.get_signal(
            candidate.learning_signals[0].id,
            organization_id=OTHER_ORGANIZATION_ID,
            user_id=OTHER_USER_ID,
        )
        is None
    )


@pytest.mark.postgres
def test_postgres_review_rows_follow_forced_tenant_policy() -> None:
    configured = explicit_test_database_url()
    if configured is None:
        pytest.skip(SKIP_REASON)
    other_organization_id = uuid4()
    candidate_id = uuid4()
    with isolated_postgres_engine(configured) as engine:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO organizations (id, name)
                    VALUES (:id, 'Other Synthetic Organization')
                    """
                ),
                {"id": other_organization_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO organization_memberships (
                        organization_id, user_id, is_active
                    )
                    VALUES (:organization_id, :user_id, TRUE)
                    """
                ),
                [
                    {
                        "organization_id": LEGACY_ORGANIZATION_ID,
                        "user_id": USER_ID,
                    },
                    {
                        "organization_id": other_organization_id,
                        "user_id": OTHER_USER_ID,
                    },
                ],
            )
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            _set_tenant(connection, USER_ID, LEGACY_ORGANIZATION_ID)
            connection.execute(
                text(
                    """
                    INSERT INTO requirement_candidates (
                        id, organization_id, provenance, classification,
                        source_document, original_discovery, review_status
                    )
                    VALUES (
                        :id, :organization_id, 'ai_inferred', 'requirement',
                        CAST(:source_document AS json),
                        CAST(:original_discovery AS json),
                        'pending_review'
                    )
                    """
                ),
                {
                    "id": candidate_id,
                    "organization_id": LEGACY_ORGANIZATION_ID,
                    "source_document": _json_document(),
                    "original_discovery": _json_original(),
                },
            )
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            _set_tenant(connection, OTHER_USER_ID, other_organization_id)
            visible = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM requirement_candidates WHERE id = :id"
                ),
                {"id": candidate_id},
            )
            assert visible == 0
            hidden_update = connection.execute(
                text(
                    """
                    UPDATE requirement_candidates
                    SET title = 'changed'
                    WHERE id = :id
                    """
                ),
                {"id": candidate_id},
            )
            assert hidden_update.rowcount == 0
        decision_id = uuid4()
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE commitmentops_app"))
            _set_tenant(connection, USER_ID, LEGACY_ORGANIZATION_ID)
            connection.execute(
                text(
                    """
                    INSERT INTO candidate_review_decisions (
                        id, organization_id, candidate_id, human_decision,
                        reviewer_user_id, reviewer_snapshot, decided_at
                    )
                    VALUES (
                        :id, :organization_id, :candidate_id, 'rejected',
                        :reviewer_user_id, CAST(:reviewer_snapshot AS json),
                        CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": decision_id,
                    "organization_id": LEGACY_ORGANIZATION_ID,
                    "candidate_id": candidate_id,
                    "reviewer_user_id": USER_ID,
                    "reviewer_snapshot": (
                        '{"id":"20000000-0000-0000-0000-000000000001",'
                        '"name":"Reviewer Example","email":"reviewer@example.test",'
                        '"role":null}'
                    ),
                },
            )
            with pytest.raises(SQLAlchemyError):
                connection.execute(
                    text(
                        """
                        UPDATE candidate_review_decisions
                        SET decision_reason = 'changed'
                        WHERE id = :id
                        """
                    ),
                    {"id": decision_id},
                )


@pytest.mark.postgres
def test_postgres_rejects_cross_organization_review_references() -> None:
    configured = explicit_test_database_url()
    if configured is None:
        pytest.skip(SKIP_REASON)
    other_organization_id = uuid4()
    candidate_id = uuid4()
    other_candidate_id = uuid4()
    decision_id = uuid4()
    with isolated_postgres_engine(configured) as engine:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO organizations (id, name)
                    VALUES (:id, 'Other Synthetic Organization')
                    """
                ),
                {"id": other_organization_id},
            )
            _insert_candidate(
                connection,
                candidate_id=candidate_id,
                organization_id=LEGACY_ORGANIZATION_ID,
            )
            _insert_candidate(
                connection,
                candidate_id=other_candidate_id,
                organization_id=other_organization_id,
            )
            _insert_decision(
                connection,
                decision_id=decision_id,
                organization_id=LEGACY_ORGANIZATION_ID,
                candidate_id=candidate_id,
                human_decision="rejected",
            )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_decision(
                    connection,
                    decision_id=uuid4(),
                    organization_id=other_organization_id,
                    candidate_id=candidate_id,
                    human_decision="rejected",
                )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_signal(
                    connection,
                    signal_id=uuid4(),
                    organization_id=other_organization_id,
                    decision_id=decision_id,
                    candidate_id=None,
                )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_signal(
                    connection,
                    signal_id=uuid4(),
                    organization_id=LEGACY_ORGANIZATION_ID,
                    decision_id=decision_id,
                    candidate_id=other_candidate_id,
                )

        human_decision_id = uuid4()
        human_signal_id = uuid4()
        with engine.begin() as connection:
            _insert_decision(
                connection,
                decision_id=human_decision_id,
                organization_id=LEGACY_ORGANIZATION_ID,
                candidate_id=None,
                human_decision="human_added",
            )
            _insert_signal(
                connection,
                signal_id=human_signal_id,
                organization_id=LEGACY_ORGANIZATION_ID,
                decision_id=human_decision_id,
                candidate_id=None,
            )
            stored_candidate_id = connection.scalar(
                text(
                    """
                    SELECT candidate_id FROM learning_signals
                    WHERE id = :id
                    """
                ),
                {"id": human_signal_id},
            )
        assert stored_candidate_id is None


def _insert_candidate(connection, *, candidate_id: UUID, organization_id: UUID) -> None:
    connection.execute(
        text(
            """
            INSERT INTO requirement_candidates (
                id, organization_id, provenance, classification,
                source_document, original_discovery, review_status
            )
            VALUES (
                :id, :organization_id, 'ai_inferred', 'requirement',
                CAST(:source_document AS json),
                CAST(:original_discovery AS json),
                'pending_review'
            )
            """
        ),
        {
            "id": candidate_id,
            "organization_id": organization_id,
            "source_document": _json_document(),
            "original_discovery": _json_original(),
        },
    )


def _insert_decision(
    connection,
    *,
    decision_id: UUID,
    organization_id: UUID,
    candidate_id: UUID | None,
    human_decision: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO candidate_review_decisions (
                id, organization_id, candidate_id, human_decision,
                reviewer_user_id, reviewer_snapshot, decided_at
            )
            VALUES (
                :id, :organization_id, :candidate_id, :human_decision,
                :reviewer_user_id, CAST(:reviewer_snapshot AS json),
                CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": decision_id,
            "organization_id": organization_id,
            "candidate_id": candidate_id,
            "human_decision": human_decision,
            "reviewer_user_id": USER_ID,
            "reviewer_snapshot": (
                '{"id":"20000000-0000-0000-0000-000000000001",'
                '"name":"Reviewer Example","email":"reviewer@example.test",'
                '"role":null}'
            ),
        },
    )


def _insert_signal(
    connection,
    *,
    signal_id: UUID,
    organization_id: UUID,
    decision_id: UUID,
    candidate_id: UUID | None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO learning_signals (
                id, organization_id, decision_id, candidate_id, source_document
            )
            VALUES (
                :id, :organization_id, :decision_id, :candidate_id,
                CAST(:source_document AS json)
            )
            """
        ),
        {
            "id": signal_id,
            "organization_id": organization_id,
            "decision_id": decision_id,
            "candidate_id": candidate_id,
            "source_document": _json_document(),
        },
    )


def _candidate() -> RequirementCandidate:
    owner = Person.create(name="Alex Rivera", email="alex.rivera@example.org")
    return RequirementCandidate.discover(
        title="Quarterly narrative",
        description="File the narrative and budget-to-actuals.",
        owner=owner,
        evidence_expectation="Signed narrative",
        provenance=RequirementProvenance.AI_INFERRED,
        source_document=_document(),
        source_location=SourceLocation(page=4, section="Reporting"),
        discovery_reason="The section uses mandatory reporting language.",
        confidence=0.5,
    )


def _document() -> SourceDocument:
    return SourceDocument.create(
        filename="award.pdf",
        document_type=DocumentType.GRANT_AGREEMENT,
        source_reference="grants/award.pdf",
        uploaded_at=datetime(2026, 10, 18, 15, 0, tzinfo=UTC),
    )


def _reviewer() -> Person:
    reviewer = Person.create(name="Reviewer Example", email="reviewer@example.test")
    return reviewer.model_copy(update={"id": USER_ID})


def _set_tenant(connection, user_id: UUID, organization_id: UUID) -> None:
    connection.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": str(user_id)},
    )
    connection.execute(
        text("SELECT set_config('app.organization_id', :organization_id, true)"),
        {"organization_id": str(organization_id)},
    )


def _json_document() -> str:
    return (
        '{"id":"00000000-0000-0000-0000-000000000010","filename":"award.pdf",'
        '"document_type":"grant_agreement","source_reference":"grants/award.pdf",'
        '"processing_status":"uploaded",'
        '"uploaded_at":"2026-10-18T15:00:00Z",'
        '"organization_reference":null}'
    )


def _json_original() -> str:
    return (
        '{"title":"Quarterly narrative","description":null,"due_date":null,'
        '"target":null,"owner":null,"supervisor":null,'
        '"evidence_expectation":null,"provenance":"ai_inferred",'
        '"classification":"requirement",'
        '"source_document_id":"00000000-0000-0000-0000-000000000010",'
        '"source_location":null,"discovery_reason":null,"confidence":null}'
    )
