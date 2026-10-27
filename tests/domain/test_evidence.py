from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain import (
    DomainValidationError,
    Evidence,
    EvidenceStatus,
    EvidenceType,
    InvalidTransitionError,
    Person,
)
from tests.domain.conftest import make_requirement


def test_evidence_association(owner: Person) -> None:
    requirement = make_requirement(owner)
    submitted_at = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
    evidence = Evidence.create(
        description="Q3 narrative PDF",
        evidence_type=EvidenceType.DOCUMENT,
        submitted_by=owner,
        reference="grants/fy26/q3-narrative.pdf",
        submitted_at=submitted_at,
    )

    requirement.add_evidence(evidence)

    assert len(requirement.evidence) == 1
    attached = requirement.evidence[0]
    assert attached.description == "Q3 narrative PDF"
    assert attached.evidence_type == EvidenceType.DOCUMENT
    assert attached.submitted_by == owner
    assert attached.reference == "grants/fy26/q3-narrative.pdf"
    assert attached.submitted_at == submitted_at
    assert attached.status == EvidenceStatus.SUBMITTED
    assert attached.identity_verified is True
    assert attached.submitter_name is None


def test_cannot_attach_the_same_evidence_twice(owner: Person) -> None:
    requirement = make_requirement(owner)
    evidence = Evidence.create(
        description="Attendance log",
        evidence_type=EvidenceType.RECORD,
        submitted_by=owner,
    )
    requirement.add_evidence(evidence)

    with pytest.raises(DomainValidationError, match="already associated"):
        requirement.add_evidence(evidence)


def test_public_evidence_is_unverified_and_has_no_person() -> None:
    evidence = Evidence(
        description="Synthetic receipt",
        evidence_type=EvidenceType.LINK,
        submitted_by=None,
        reference="https://example.test/receipt",
        submitter_name="Synthetic Submitter",
        identity_verified=False,
    )

    assert evidence.submitted_by is None
    assert evidence.identity_verified is False
    assert evidence.submitter_name == "Synthetic Submitter"


def test_verified_evidence_requires_a_person() -> None:
    with pytest.raises(ValidationError):
        Evidence(
            description="Synthetic receipt",
            evidence_type=EvidenceType.LINK,
            submitted_by=None,
            identity_verified=True,
        )


def test_cannot_attach_evidence_to_cancelled_requirement(owner: Person) -> None:
    requirement = make_requirement(owner)
    requirement.cancel()

    with pytest.raises(InvalidTransitionError, match="cancelled requirement"):
        requirement.add_evidence(
            Evidence.create(
                description="Late note",
                evidence_type=EvidenceType.NOTE,
                submitted_by=owner,
            )
        )
