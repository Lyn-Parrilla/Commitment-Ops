from datetime import UTC, date, datetime

import pytest

from app.domain import (
    DiscoveryClassification,
    DocumentType,
    Person,
    Requirement,
    RequirementCandidate,
    RequirementProvenance,
    SourceDocument,
    SourceLocation,
)


@pytest.fixture
def owner() -> Person:
    return Person.create(
        name="Alex Rivera",
        email="alex.rivera@example.org",
        role="Program Manager",
    )


@pytest.fixture
def supervisor() -> Person:
    return Person.create(
        name="Jordan Lee",
        email="jordan.lee@example.org",
        role="Director",
    )


@pytest.fixture
def reviewer() -> Person:
    return Person.create(
        name="Sam Okonkwo",
        email="sam.okonkwo@example.org",
        role="Compliance Officer",
    )


@pytest.fixture
def source_document() -> SourceDocument:
    return SourceDocument.create(
        filename="synthetic-award-notice.pdf",
        document_type=DocumentType.GRANT_AGREEMENT,
        source_reference="intake/synthetic-award-notice.pdf",
        uploaded_at=datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
        organization_reference="synthetic-community-services",
    )


def make_requirement(
    owner: Person,
    *,
    provenance: RequirementProvenance = RequirementProvenance.USER_PROVIDED,
    supervisor: Person | None = None,
) -> Requirement:
    return Requirement.create(
        title="Submit quarterly progress report",
        description="File the Q3 narrative and budget-to-actuals with the funder.",
        owner=owner,
        supervisor=supervisor,
        evidence_expectation="Signed PDF uploaded to the grant folder",
        provenance=provenance,
        due_date=date(2026, 12, 15),
        target="1 report",
    )


def make_candidate(
    source_document: SourceDocument,
    owner: Person | None,
    *,
    provenance: RequirementProvenance = RequirementProvenance.AI_INFERRED,
) -> RequirementCandidate:
    return RequirementCandidate.discover(
        title="Quarterly Outcomes Report",
        description="Submit program outcomes for the reporting quarter.",
        due_date=date(2026, 12, 15),
        target="50 participants served",
        owner=owner,
        evidence_expectation="Quarterly participant records",
        provenance=provenance,
        classification=DiscoveryClassification.DELIVERABLE,
        source_document=source_document,
        source_location=SourceLocation(
            page=7,
            section="Reporting Requirements",
            heading="Quarterly Outcomes",
        ),
        discovery_reason="The section uses mandatory reporting language.",
        confidence=0.86,
    )
