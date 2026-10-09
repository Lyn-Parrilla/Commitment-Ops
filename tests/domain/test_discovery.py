from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain import (
    AccountabilityField,
    CandidateRevision,
    DiscoveryClassification,
    DocumentProcessingStatus,
    DocumentType,
    DomainValidationError,
    FieldPresence,
    InvalidTransitionError,
    Person,
    RequirementCandidate,
    RequirementProvenance,
    ReviewStatus,
    SourceDocument,
    SourceLocation,
)
from tests.domain.conftest import make_candidate


def test_creating_a_source_document() -> None:
    uploaded_at = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)

    document = SourceDocument.create(
        filename="synthetic-contract.pdf",
        document_type=DocumentType.CONTRACT,
        source_reference="manual-upload/synthetic-contract.pdf",
        uploaded_at=uploaded_at,
        organization_reference="synthetic-operations-team",
    )

    assert document.id is not None
    assert document.filename == "synthetic-contract.pdf"
    assert document.document_type == DocumentType.CONTRACT
    assert document.source_reference == "manual-upload/synthetic-contract.pdf"
    assert document.processing_status == DocumentProcessingStatus.UPLOADED
    assert document.uploaded_at == uploaded_at
    assert document.organization_reference == "synthetic-operations-team"


@pytest.mark.parametrize("field", ["filename", "source_reference"])
def test_source_document_requires_traceability_fields(field: str) -> None:
    values = {
        "filename": "synthetic-policy.pdf",
        "document_type": DocumentType.POLICY,
        "source_reference": "manual-upload/synthetic-policy.pdf",
    }
    values[field] = " "

    with pytest.raises(DomainValidationError, match=f"{field} is required"):
        SourceDocument.create(**values)  # type: ignore[arg-type]


def test_source_document_processing_lifecycle(
    source_document: SourceDocument,
) -> None:
    source_document.start_processing()
    assert source_document.processing_status == DocumentProcessingStatus.PROCESSING

    source_document.mark_processed()
    assert source_document.processing_status == DocumentProcessingStatus.PROCESSED

    with pytest.raises(InvalidTransitionError, match="cannot start processing"):
        source_document.start_processing()


def test_failed_document_can_be_retried(source_document: SourceDocument) -> None:
    source_document.start_processing()
    source_document.mark_failed()
    source_document.start_processing()

    assert source_document.processing_status == DocumentProcessingStatus.PROCESSING


def test_source_location_requires_known_information() -> None:
    with pytest.raises(ValidationError, match="source location detail"):
        SourceLocation()


def test_valid_candidate_preserves_provenance_and_source_traceability(
    source_document: SourceDocument,
    owner: Person,
) -> None:
    candidate = make_candidate(source_document, owner)

    assert candidate.title == "Quarterly Outcomes Report"
    assert candidate.classification == DiscoveryClassification.DELIVERABLE
    assert candidate.provenance == RequirementProvenance.AI_INFERRED
    assert candidate.source_document.id == source_document.id
    assert candidate.source_location is not None
    assert candidate.source_location.page == 7
    assert candidate.discovery_reason == (
        "The section uses mandatory reporting language."
    )
    assert candidate.confidence == 0.86
    assert candidate.review_status == ReviewStatus.PENDING_REVIEW
    assert candidate.is_authoritative is False


def test_candidate_captures_source_metadata_at_discovery_time(
    source_document: SourceDocument,
    owner: Person,
) -> None:
    candidate = make_candidate(source_document, owner)
    snapshot = candidate.source_document_snapshot

    assert snapshot.id == source_document.id
    assert snapshot.filename == "synthetic-award-notice.pdf"
    assert snapshot.document_type == DocumentType.GRANT_AGREEMENT
    assert snapshot.source_reference == "intake/synthetic-award-notice.pdf"
    assert snapshot.processing_status == DocumentProcessingStatus.UPLOADED
    assert snapshot.uploaded_at == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    assert snapshot.organization_reference == "synthetic-community-services"


def test_source_changes_do_not_rewrite_candidate_discovery_snapshot(
    source_document: SourceDocument,
    owner: Person,
) -> None:
    candidate = make_candidate(source_document, owner)

    source_document.filename = "renamed-after-discovery.pdf"
    source_document.document_type = DocumentType.OTHER
    source_document.source_reference = "reclassified/renamed-after-discovery.pdf"
    source_document.processing_status = DocumentProcessingStatus.PROCESSED
    source_document.uploaded_at = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
    source_document.organization_reference = "changed-organization"

    snapshot = candidate.source_document_snapshot
    assert snapshot.filename == "synthetic-award-notice.pdf"
    assert snapshot.document_type == DocumentType.GRANT_AGREEMENT
    assert snapshot.source_reference == "intake/synthetic-award-notice.pdf"
    assert snapshot.processing_status == DocumentProcessingStatus.UPLOADED
    assert snapshot.uploaded_at == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    assert snapshot.organization_reference == "synthetic-community-services"


def test_candidate_revision_does_not_replace_original_source_snapshot(
    source_document: SourceDocument,
    owner: Person,
) -> None:
    candidate = make_candidate(source_document, owner)
    original_snapshot = candidate.source_document_snapshot

    candidate.revise(CandidateRevision(target="75 participants served"))

    assert candidate.source_document_snapshot is original_snapshot
    assert candidate.source_document_snapshot.filename == (
        "synthetic-award-notice.pdf"
    )
    assert candidate.review_status == ReviewStatus.PENDING_REVIEW
    assert candidate.is_authoritative is False


def test_candidate_source_snapshot_cannot_be_mutated_indirectly(
    source_document: SourceDocument,
    owner: Person,
) -> None:
    candidate = make_candidate(source_document, owner)

    with pytest.raises(ValidationError, match="frozen"):
        candidate.source_document_snapshot.filename = (  # type: ignore[misc]
            "mutated-through-candidate.pdf"
        )

    assert candidate.source_document_snapshot.filename == (
        "synthetic-award-notice.pdf"
    )


def test_complete_candidate_distinguishes_optional_missing_information(
    source_document: SourceDocument,
    owner: Person,
) -> None:
    candidate = RequirementCandidate.discover(
        title="Submit compliance certification",
        description="Provide the signed annual certification.",
        owner=owner,
        evidence_expectation="Signed certification",
        provenance=RequirementProvenance.DOCUMENT_EXPLICIT,
        source_document=source_document,
    )

    assessment = candidate.assess_completeness()

    assert assessment.is_complete is True
    assert assessment.required_missing == ()
    assert assessment.optional_missing == (
        AccountabilityField.DUE_DATE,
        AccountabilityField.TARGET,
        AccountabilityField.SUPERVISOR,
    )
    assert (
        assessment.presence_of(AccountabilityField.OWNER)
        == FieldPresence.PRESENT
    )
    assert (
        assessment.presence_of(AccountabilityField.DUE_DATE)
        == FieldPresence.OPTIONAL_MISSING
    )


def test_incomplete_candidate_reports_required_and_optional_missing_information(
    source_document: SourceDocument,
) -> None:
    candidate = RequirementCandidate.discover(
        title="Potential annual report",
        provenance=RequirementProvenance.AI_INFERRED,
        source_document=source_document,
        discovery_reason="A heading resembles a known reporting term.",
    )

    assessment = candidate.assess_completeness()

    assert assessment.is_complete is False
    assert assessment.required_missing == (
        AccountabilityField.DESCRIPTION,
        AccountabilityField.OWNER,
        AccountabilityField.EVIDENCE_EXPECTATION,
    )
    assert assessment.optional_missing == (
        AccountabilityField.DUE_DATE,
        AccountabilityField.TARGET,
        AccountabilityField.SUPERVISOR,
    )


def test_candidate_does_not_invent_missing_information(
    source_document: SourceDocument,
) -> None:
    candidate = RequirementCandidate.discover(
        title=None,
        description=None,
        owner=None,
        evidence_expectation=None,
        provenance=RequirementProvenance.AI_INFERRED,
        source_document=source_document,
    )

    assert candidate.title is None
    assert candidate.description is None
    assert candidate.owner is None
    assert candidate.evidence_expectation is None


def test_ai_inferred_and_document_explicit_candidates_remain_distinguishable(
    source_document: SourceDocument,
    owner: Person,
) -> None:
    inferred = make_candidate(source_document, owner)
    explicit = make_candidate(
        source_document,
        owner,
        provenance=RequirementProvenance.DOCUMENT_EXPLICIT,
    )

    assert inferred.provenance == RequirementProvenance.AI_INFERRED
    assert explicit.provenance == RequirementProvenance.DOCUMENT_EXPLICIT
    assert inferred.review_status == explicit.review_status == ReviewStatus.PENDING_REVIEW
    assert inferred.is_authoritative is explicit.is_authoritative is False


def test_candidate_confidence_must_be_between_zero_and_one(
    source_document: SourceDocument,
) -> None:
    with pytest.raises(ValidationError):
        RequirementCandidate.discover(
            provenance=RequirementProvenance.AI_INFERRED,
            source_document=source_document,
            confidence=1.1,
        )
