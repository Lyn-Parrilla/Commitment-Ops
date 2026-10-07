from datetime import date

import pytest

from app.domain import (
    DomainValidationError,
    InvalidTransitionError,
    Person,
    Requirement,
    RequirementProvenance,
    RequirementStatus,
    ReviewStatus,
)
from tests.domain.conftest import make_requirement


def test_creating_a_valid_requirement(owner: Person, supervisor: Person) -> None:
    requirement = Requirement.create(
        title="Complete site visit",
        description="Host the funder's annual monitoring visit.",
        owner=owner,
        supervisor=supervisor,
        evidence_expectation="Signed site-visit checklist",
        provenance=RequirementProvenance.ORGANIZATION_TEMPLATE,
        due_date=date(2026, 11, 1),
        target="1 visit",
    )

    assert requirement.title == "Complete site visit"
    assert requirement.owner == owner
    assert requirement.supervisor == supervisor
    assert requirement.due_date == date(2026, 11, 1)
    assert requirement.target == "1 visit"
    assert requirement.status == RequirementStatus.NOT_STARTED
    assert requirement.is_authoritative is True
    assert requirement.review_status == ReviewStatus.APPROVED


def test_assigning_an_owner(owner: Person) -> None:
    requirement = make_requirement(owner)
    new_owner = Person.create(name="Casey Nguyen", email="casey.nguyen@example.org")

    requirement.assign_owner(new_owner)

    assert requirement.owner.email == "casey.nguyen@example.org"


def test_requirement_provenance_is_recorded(owner: Person) -> None:
    for provenance in RequirementProvenance:
        requirement = make_requirement(owner, provenance=provenance)
        assert requirement.provenance == provenance


def test_ai_inferred_requirements_are_not_authoritative(owner: Person) -> None:
    requirement = make_requirement(owner, provenance=RequirementProvenance.AI_INFERRED)

    assert requirement.review_status == ReviewStatus.PENDING_REVIEW
    assert requirement.is_authoritative is False


def test_ai_inferred_requirements_cannot_be_created_approved(owner: Person) -> None:
    with pytest.raises(DomainValidationError, match="cannot be created as authoritative"):
        Requirement.create(
            title="Inferred reporting clause",
            description="Model suggested an extra narrative report.",
            owner=owner,
            evidence_expectation="Funder confirmation email",
            provenance=RequirementProvenance.AI_INFERRED,
            review_status=ReviewStatus.APPROVED,
        )


def test_missing_required_information(owner: Person) -> None:
    with pytest.raises(DomainValidationError, match="title is required"):
        Requirement.create(
            title=" ",
            description="A real description",
            owner=owner,
            evidence_expectation="A file",
            provenance=RequirementProvenance.USER_PROVIDED,
        )

    with pytest.raises(DomainValidationError, match="owner is required"):
        Requirement.create(
            title="Named requirement",
            description="A real description",
            owner=None,  # type: ignore[arg-type]
            evidence_expectation="A file",
            provenance=RequirementProvenance.USER_PROVIDED,
        )

    with pytest.raises(DomainValidationError, match="evidence_expectation is required"):
        Requirement.create(
            title="Named requirement",
            description="A real description",
            owner=owner,
            evidence_expectation="",
            provenance=RequirementProvenance.USER_PROVIDED,
        )


def test_requirement_review_approval(owner: Person, reviewer: Person) -> None:
    requirement = make_requirement(owner, provenance=RequirementProvenance.AI_INFERRED)

    requirement.approve(reviewer=reviewer, note="Matches the grant agreement")

    assert requirement.review_status == ReviewStatus.APPROVED
    assert requirement.is_authoritative is True
    assert requirement.reviewed_by == reviewer
    assert requirement.reviewed_at is not None
    assert requirement.review_note == "Matches the grant agreement"


def test_requirement_rejection(owner: Person, reviewer: Person) -> None:
    requirement = make_requirement(owner, provenance=RequirementProvenance.AI_INFERRED)

    requirement.reject(reviewer=reviewer, reason="Not stated in the award")

    assert requirement.review_status == ReviewStatus.REJECTED
    assert requirement.is_authoritative is False
    assert requirement.reviewed_by == reviewer
    assert requirement.review_note == "Not stated in the award"


def test_rejection_requires_a_reason(owner: Person, reviewer: Person) -> None:
    requirement = make_requirement(owner, provenance=RequirementProvenance.AI_INFERRED)

    with pytest.raises(DomainValidationError, match="reason is required"):
        requirement.reject(reviewer=reviewer, reason=" ")


def test_invalid_review_transitions(owner: Person, reviewer: Person) -> None:
    requirement = make_requirement(owner, provenance=RequirementProvenance.USER_PROVIDED)

    with pytest.raises(InvalidTransitionError, match="cannot approve"):
        requirement.approve(reviewer=reviewer)

    pending = make_requirement(owner, provenance=RequirementProvenance.AI_INFERRED)
    pending.reject(reviewer=reviewer, reason="Out of scope")

    with pytest.raises(InvalidTransitionError, match="cannot reject"):
        pending.reject(reviewer=reviewer, reason="Already rejected")

    pending.submit_for_review()
    pending.approve(reviewer=reviewer)

    with pytest.raises(InvalidTransitionError, match="cannot reject"):
        pending.reject(reviewer=reviewer, reason="Too late")


def test_invalid_work_status_transitions(owner: Person) -> None:
    pending = make_requirement(owner, provenance=RequirementProvenance.AI_INFERRED)
    with pytest.raises(InvalidTransitionError, match="must be approved"):
        pending.start_work()

    requirement = make_requirement(owner)
    with pytest.raises(InvalidTransitionError, match="cannot change requirement status"):
        requirement.block()

    requirement.start_work()
    requirement.complete()

    with pytest.raises(InvalidTransitionError, match="cannot change requirement status"):
        requirement.start_work()
