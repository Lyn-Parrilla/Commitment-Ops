from datetime import date

import pytest

from app.domain import Person, Requirement, RequirementProvenance


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
