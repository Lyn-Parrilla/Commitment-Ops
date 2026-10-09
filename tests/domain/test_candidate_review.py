import pytest

from app.domain import (
    CandidateRevision,
    DomainValidationError,
    HumanDecision,
    InvalidTransitionError,
    LearningSignal,
    Person,
    Requirement,
    RequirementCandidate,
    RequirementProvenance,
    RequirementStatus,
    ReviewStatus,
    SourceDocument,
)
from tests.domain.conftest import make_candidate, make_requirement


def test_approving_candidate_creates_authoritative_requirement(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    candidate = make_candidate(source_document, owner)

    approval = candidate.approve(
        reviewer=reviewer,
        note="Confirmed against the reporting clause.",
    )

    assert isinstance(approval.requirement, Requirement)
    assert approval.requirement.is_authoritative is True
    assert approval.requirement.review_status == ReviewStatus.APPROVED
    assert approval.requirement.reviewed_by == reviewer
    assert approval.requirement.provenance == RequirementProvenance.AI_INFERRED
    assert candidate.review_status == ReviewStatus.APPROVED
    assert candidate.is_authoritative is False
    assert approval.learning_signal.human_decision == HumanDecision.APPROVED
    assert (
        approval.learning_signal.resulting_requirement.requirement_id
        == approval.requirement.id
    )


def test_incomplete_candidate_cannot_cross_authority_boundary(
    source_document: SourceDocument,
    reviewer: Person,
) -> None:
    candidate = RequirementCandidate.discover(
        title="Potential report",
        provenance=RequirementProvenance.AI_INFERRED,
        source_document=source_document,
    )

    with pytest.raises(
        DomainValidationError,
        match="description, owner, evidence_expectation",
    ):
        candidate.approve(reviewer=reviewer)

    assert candidate.review_status == ReviewStatus.PENDING_REVIEW
    assert candidate.learning_signals == []


def test_candidate_review_requires_a_human_reviewer(
    source_document: SourceDocument,
    owner: Person,
) -> None:
    candidate = make_candidate(source_document, owner)

    with pytest.raises(DomainValidationError, match="reviewer is required"):
        candidate.approve(reviewer=None)  # type: ignore[arg-type]

    assert candidate.review_status == ReviewStatus.PENDING_REVIEW
    assert candidate.learning_signals == []


def test_rejecting_candidate_does_not_create_requirement(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    candidate = make_candidate(source_document, owner)

    signal = candidate.reject(
        reviewer=reviewer,
        reason="The paragraph describes an example, not an obligation.",
    )

    assert candidate.review_status == ReviewStatus.REJECTED
    assert candidate.is_authoritative is False
    assert signal.human_decision == HumanDecision.REJECTED
    assert signal.resulting_requirement is None
    assert signal.decision_reason == (
        "The paragraph describes an example, not an obligation."
    )


def test_edit_after_rejection_resubmits_without_bypassing_review(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    candidate = make_candidate(source_document, owner)
    candidate.reject(reviewer=reviewer, reason="Owner classification is incorrect.")
    corrected_owner = Person.create(
        name="Morgan Patel",
        email="morgan.patel@example.org",
        role="Grants Director",
    )

    candidate.revise(CandidateRevision(owner=corrected_owner))

    assert candidate.review_status == ReviewStatus.PENDING_REVIEW
    assert candidate.reviewed_by is None
    assert candidate.reviewed_at is None
    assert candidate.is_authoritative is False
    assert len(candidate.learning_signals) == 1

    approval = candidate.approve(reviewer=reviewer)

    assert approval.requirement.owner == corrected_owner
    assert len(candidate.learning_signals) == 2
    assert [signal.human_decision for signal in candidate.learning_signals] == [
        HumanDecision.REJECTED,
        HumanDecision.APPROVED,
    ]


def test_edit_while_pending_remains_pending_review(
    source_document: SourceDocument,
    owner: Person,
) -> None:
    candidate = make_candidate(source_document, owner)

    candidate.revise(
        CandidateRevision(evidence_expectation="Signed quarterly participant records")
    )

    assert candidate.review_status == ReviewStatus.PENDING_REVIEW
    assert candidate.is_authoritative is False
    assert candidate.learning_signals == []


def test_approved_candidate_cannot_be_revised_or_reviewed_again(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    candidate = make_candidate(source_document, owner)
    candidate.approve(reviewer=reviewer)

    with pytest.raises(InvalidTransitionError, match="cannot revise"):
        candidate.revise(CandidateRevision(target="75 participants"))
    with pytest.raises(InvalidTransitionError, match="cannot approve"):
        candidate.approve(reviewer=reviewer)
    with pytest.raises(InvalidTransitionError, match="cannot reject"):
        candidate.reject(reviewer=reviewer, reason="Too late")


def test_existing_requirement_lifecycle_still_applies_after_promotion(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    requirement = make_candidate(source_document, owner).approve(
        reviewer=reviewer
    ).requirement

    requirement.start_work()
    requirement.complete()

    assert requirement.status == RequirementStatus.COMPLETED
    with pytest.raises(InvalidTransitionError, match="cannot change requirement status"):
        requirement.start_work()


def test_original_discovery_is_preserved_after_human_correction(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    candidate = make_candidate(source_document, owner)
    corrected_owner = Person.create(
        name="Morgan Patel",
        email="morgan.patel@example.org",
        role="Grants Director",
    )
    candidate.revise(
        CandidateRevision(
            owner=corrected_owner,
            evidence_expectation="Signed quarterly participant records",
        )
    )

    signal = candidate.approve(reviewer=reviewer).learning_signal

    assert signal.original_discovery.owner.id == owner.id
    assert (
        signal.original_discovery.evidence_expectation
        == "Quarterly participant records"
    )
    assert signal.reviewed_candidate.owner.id == corrected_owner.id
    assert (
        signal.reviewed_candidate.evidence_expectation
        == "Signed quarterly participant records"
    )
    assert signal.corrected_fields == ("owner", "evidence_expectation")
    assert signal.is_human_correction is True


def test_learning_signal_preserves_original_discovery_metadata(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    candidate = make_candidate(source_document, owner)

    signal = candidate.approve(reviewer=reviewer).learning_signal

    assert signal.candidate_id == candidate.id
    assert signal.source_document.id == source_document.id
    assert signal.source_document.document_type == source_document.document_type
    assert signal.source_location == candidate.source_location
    assert signal.discovery_provenance == RequirementProvenance.AI_INFERRED
    assert signal.discovery_reason == (
        "The section uses mandatory reporting language."
    )
    assert signal.original_confidence == 0.86
    assert signal.original_discovery.provenance == RequirementProvenance.AI_INFERRED
    assert signal.human_decision == HumanDecision.APPROVED


def test_learning_signal_uses_discovery_time_source_snapshot_after_correction(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    candidate = make_candidate(source_document, owner)
    corrected_owner = Person.create(
        name="Morgan Patel",
        email="morgan.patel@example.org",
        role="Grants Director",
    )

    source_document.filename = "renamed-before-review.pdf"
    source_document.source_reference = "changed/renamed-before-review.pdf"
    source_document.organization_reference = "changed-organization"
    candidate.revise(CandidateRevision(owner=corrected_owner))

    approval = candidate.approve(reviewer=reviewer)
    signal = approval.learning_signal

    assert signal.source_document == candidate.source_document_snapshot
    assert signal.source_document.filename == "synthetic-award-notice.pdf"
    assert (
        signal.source_document.source_reference
        == "intake/synthetic-award-notice.pdf"
    )
    assert (
        signal.source_document.organization_reference
        == "synthetic-community-services"
    )
    assert signal.reviewed_candidate.owner.id == corrected_owner.id
    assert signal.original_discovery.owner.id == owner.id
    assert signal.human_decision == HumanDecision.APPROVED
    assert approval.requirement.is_authoritative is True


def test_rejection_is_preserved_as_validated_feedback(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    candidate = make_candidate(source_document, owner)

    signal = candidate.reject(reviewer=reviewer, reason="Not an obligation.")

    assert signal.original_discovery is not None
    assert signal.reviewed_candidate is not None
    assert signal.human_decision == HumanDecision.REJECTED
    assert signal.reviewer.id == reviewer.id
    assert signal.resulting_requirement is None
    assert candidate.learning_signals == [signal]


def test_human_added_requirement_can_be_recorded_as_learning_signal(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    requirement = make_requirement(
        owner,
        provenance=RequirementProvenance.DOCUMENT_EXPLICIT,
    )

    signal = LearningSignal.record_human_added(
        source_document=source_document,
        source_location=None,
        requirement=requirement,
        reviewer=reviewer,
        reason="Reviewer found a deliverable discovery had missed.",
    )

    assert signal.candidate_id is None
    assert signal.original_discovery is None
    assert signal.human_decision == HumanDecision.HUMAN_ADDED
    assert signal.resulting_requirement.requirement_id == requirement.id
    assert (
        signal.resulting_requirement.provenance
        == RequirementProvenance.DOCUMENT_EXPLICIT
    )


def test_learning_signal_cannot_record_non_authoritative_human_added_requirement(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    pending_requirement = make_requirement(
        owner,
        provenance=RequirementProvenance.AI_INFERRED,
    )

    with pytest.raises(DomainValidationError, match="authoritative requirement"):
        LearningSignal.record_human_added(
            source_document=source_document,
            requirement=pending_requirement,
            reviewer=reviewer,
        )


def test_learning_signal_is_not_an_authoritative_requirement_or_training_action(
    source_document: SourceDocument,
    owner: Person,
    reviewer: Person,
) -> None:
    signal = make_candidate(source_document, owner).reject(
        reviewer=reviewer,
        reason="Not an obligation.",
    )

    assert not isinstance(signal, Requirement)
    assert not hasattr(signal, "approve")
    assert not hasattr(signal, "train")
    assert not hasattr(signal, "retrain")
