"""Upload a source document, interpret it, and require a human decision.

AI candidates stay non-authoritative. Approval is the only path that adds a
requirement to the workflow, and the reviewer is the authenticated principal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.application.discovery import (
    CandidateReviewAction,
    DiscoveryService,
)
from app.application.exceptions import (
    CandidateNotFoundError,
    ResourceNotFoundError,
    WorkflowNotFoundError,
)
from app.application.document_interpretation import (
    DocumentInterpretationError,
    DocumentInterpreter,
)
from app.domain.discovery import (
    DocumentProcessingStatus,
    DocumentType,
    SourceDocument,
    SourceLocation,
)
from app.domain.document_text import (
    ExtractedPage,
    extract_document_text,
    prompt_window,
)
from app.domain.enums import RequirementProvenance, WorkflowStatus
from app.domain.evidence_files import (
    InspectedEvidenceFile,
    build_source_document_key,
)
from app.domain.exceptions import DomainValidationError, InvalidTransitionError
from app.domain.models import Person, Requirement
from app.persistence.exceptions import PersistenceError
from app.persistence.object_storage import ObjectStorage
from app.persistence.repository import WorkflowRepository
from app.persistence.source_documents import (
    SourceDocumentRepository,
    StoredSourceDocument,
)


class SourceDocumentNotFoundError(ResourceNotFoundError):
    def __init__(self, document_id: UUID) -> None:
        super().__init__(f"source document {document_id} was not found")


@dataclass(frozen=True)
class UploadedSourceDocument:
    document: StoredSourceDocument
    duplicate: bool


class DocumentIntakeService:
    def __init__(
        self,
        documents: SourceDocumentRepository,
        discovery: DiscoveryService,
        workflows: WorkflowRepository,
        storage: ObjectStorage,
        interpreter: DocumentInterpreter,
        *,
        organization_id: UUID,
        user_id: UUID,
        reviewer: Person,
        actor_reference: str | None,
    ) -> None:
        self._documents = documents
        self._discovery = discovery
        self._workflows = workflows
        self._storage = storage
        self._interpreter = interpreter
        self._organization_id = organization_id
        self._user_id = user_id
        self._reviewer = reviewer
        self._actor_reference = actor_reference

    def upload(
        self,
        workflow_id: UUID,
        inspected: InspectedEvidenceFile,
        document_type: DocumentType,
    ) -> UploadedSourceDocument:
        self._require_workflow(workflow_id)
        existing = self._documents.find_duplicate(
            organization_id=self._organization_id,
            workflow_id=workflow_id,
            checksum_sha256=inspected.checksum_sha256,
            user_id=self._user_id,
        )
        if existing is not None:
            return UploadedSourceDocument(document=existing, duplicate=True)
        document_id = uuid4()
        key = build_source_document_key(
            str(self._organization_id),
            str(workflow_id),
            str(document_id),
        )
        self._storage.put_new(key, inspected.content, inspected.media_type)
        document = StoredSourceDocument(
            id=document_id,
            organization_id=self._organization_id,
            workflow_id=workflow_id,
            filename=inspected.filename,
            media_type=inspected.media_type,
            byte_size=inspected.byte_size,
            checksum_sha256=inspected.checksum_sha256,
            document_type=document_type,
            storage_key=key,
            processing_status=DocumentProcessingStatus.UPLOADED,
            failure_code=None,
            text_truncated=False,
            pages=(),
            uploaded_by_user_id=self._user_id,
            uploaded_at=datetime.now(UTC),
        )
        try:
            self._documents.add(
                document,
                user_id=self._user_id,
                actor_reference=self._actor_reference,
            )
        except PersistenceError:
            self._storage.delete(key)
            raise
        return UploadedSourceDocument(document=document, duplicate=False)

    def list_documents(self, workflow_id: UUID) -> list[StoredSourceDocument]:
        self._require_workflow(workflow_id)
        return self._documents.list_for_workflow(
            organization_id=self._organization_id,
            workflow_id=workflow_id,
            user_id=self._user_id,
        )

    def get_document(
        self,
        workflow_id: UUID,
        document_id: UUID,
    ) -> StoredSourceDocument:
        self._require_workflow(workflow_id)
        document = self._documents.get(
            document_id,
            organization_id=self._organization_id,
            workflow_id=workflow_id,
            user_id=self._user_id,
        )
        if document is None:
            raise SourceDocumentNotFoundError(document_id)
        return document

    def excerpt(
        self,
        workflow_id: UUID,
        document_id: UUID,
        *,
        page: int | None,
        section: str | None,
    ) -> ExtractedPage:
        document = self.get_document(workflow_id, document_id)
        if not document.pages:
            raise DomainValidationError("Source context is not available yet.")
        chosen = None
        for item in document.pages:
            if page is not None and item.page == page:
                chosen = item
                break
            if section and section.lower() in item.text.lower():
                chosen = item
                break
        if chosen is None:
            chosen = document.pages[0]
        text = chosen.text[:2_000]
        return ExtractedPage(page=chosen.page, text=text)

    def process(self, workflow_id: UUID, document_id: UUID) -> StoredSourceDocument:
        document = self.get_document(workflow_id, document_id)
        if document.processing_status == DocumentProcessingStatus.PROCESSED:
            return document
        source = _source(document)
        try:
            if document.processing_status == DocumentProcessingStatus.PROCESSING:
                source.mark_failed()
            source.start_processing()
        except InvalidTransitionError:
            return document
        document = _replace(document, status=source.processing_status, failure_code=None)
        self._save(document, "source_document_processing")
        try:
            content = self._storage.get(document.storage_key)
            extracted = extract_document_text(document.media_type, content)
            if not extracted.has_text:
                return self._fail(document, source, "no_extractable_text")
            model_pages, prompt_truncated = prompt_window(extracted.pages)
            drafts = self._interpreter.interpret(model_pages)
            existing = {
                (
                    candidate.title,
                    candidate.provenance,
                    None
                    if candidate.source_location is None
                    else candidate.source_location.page,
                )
                for candidate in self._discovery.list_candidates()
                if candidate.source_document.id == document.id
            }
            for draft in drafts:
                identity = (draft.title, draft.provenance, draft.location.page)
                if identity in existing:
                    continue
                reason = draft.explanation
                if draft.uncertain:
                    reason = f"Uncertain. {reason}"
                if extracted.truncated or prompt_truncated:
                    reason = f"{reason} Source text was truncated."
                self._discovery.discover_candidate(
                    source_document=_source(
                        _replace(
                            document,
                            status=DocumentProcessingStatus.PROCESSED,
                        )
                    ),
                    provenance=draft.provenance,
                    title=draft.title,
                    description=draft.description,
                    due_date=draft.due_date,
                    target=draft.target,
                    evidence_expectation=draft.evidence_expectation,
                    classification=draft.classification,
                    source_location=draft.location,
                    discovery_reason=reason[:500],
                    confidence=draft.confidence,
                )
                existing.add(identity)
            finished = source
            finished.mark_processed()
            document = _replace(
                document,
                status=finished.processing_status,
                failure_code=None,
                pages=extracted.pages,
                text_truncated=extracted.truncated or prompt_truncated,
            )
            self._save(document, "source_document_processed")
            return document
        except DocumentInterpretationError as exc:
            return self._fail(document, source, exc.code)
        except PersistenceError:
            self._fail(document, source, "interpretation_failed")
            raise

    def candidates(self, workflow_id: UUID, document_id: UUID):
        document = self.get_document(workflow_id, document_id)
        return [
            candidate
            for candidate in self._discovery.list_candidates()
            if candidate.source_document.id == document.id
        ]

    def revise_candidate(
        self,
        workflow_id: UUID,
        document_id: UUID,
        candidate_id: UUID,
        revision,
    ):
        self._require_candidate(workflow_id, document_id, candidate_id)
        return self._discovery.revise_candidate(candidate_id, revision)

    def review_candidate(
        self,
        workflow_id: UUID,
        document_id: UUID,
        candidate_id: UUID,
        action: CandidateReviewAction,
        *,
        note: str | None = None,
        reason: str | None = None,
    ):
        self._require_candidate(workflow_id, document_id, candidate_id)
        if action == CandidateReviewAction.APPROVE:
            workflow = self._require_workflow(workflow_id)
            if workflow.status in {
                WorkflowStatus.COMPLETED,
                WorkflowStatus.CANCELLED,
            }:
                raise InvalidTransitionError(
                    "cannot add requirements to a closed workflow"
                )
        reviewed = self._discovery.review_candidate(
            candidate_id,
            action,
            note=note,
            reason=reason,
        )
        if reviewed.requirement is not None:
            workflow = self._require_workflow(workflow_id)
            workflow.add_requirement(reviewed.requirement)
            self._workflows.save(
                workflow,
                user_id=self._user_id,
                organization_id=self._organization_id,
                actor_reference=self._actor_reference,
            )
        self._save(
            self.get_document(workflow_id, document_id),
            "source_document_reviewed",
        )
        return reviewed.candidate

    def add_missing_requirement(
        self,
        workflow_id: UUID,
        document_id: UUID,
        *,
        title: str,
        description: str,
        owner: Person,
        evidence_expectation: str,
        due_date=None,
        target: str | None = None,
        supervisor: Person | None = None,
        source_location: SourceLocation | None = None,
        reason: str | None = None,
    ):
        document = self.get_document(workflow_id, document_id)
        requirement = Requirement.create(
            title=title,
            description=description,
            owner=owner,
            supervisor=supervisor,
            evidence_expectation=evidence_expectation,
            provenance=RequirementProvenance.USER_PROVIDED,
            due_date=due_date,
            target=target,
        )
        workflow = self._require_workflow(workflow_id)
        workflow.add_requirement(requirement)
        self._workflows.save(
            workflow,
            user_id=self._user_id,
            organization_id=self._organization_id,
            actor_reference=self._actor_reference,
        )
        self._discovery.record_human_added(
            source_document=_source(document),
            requirement=requirement,
            source_location=source_location,
            reason=reason,
        )
        return requirement

    def _fail(self, document, source: SourceDocument, code: str) -> StoredSourceDocument:
        if source.processing_status != DocumentProcessingStatus.PROCESSING:
            try:
                source.start_processing()
            except InvalidTransitionError:
                source = _source(document)
                if source.processing_status == DocumentProcessingStatus.UPLOADED:
                    source.start_processing()
        if source.processing_status == DocumentProcessingStatus.PROCESSING:
            source.mark_failed()
        failed = _replace(
            document,
            status=DocumentProcessingStatus.FAILED,
            failure_code=code,
        )
        self._save(failed, "source_document_failed")
        return failed

    def _require_candidate(self, workflow_id: UUID, document_id: UUID, candidate_id: UUID):
        self.get_document(workflow_id, document_id)
        matches = [
            candidate
            for candidate in self.candidates(workflow_id, document_id)
            if candidate.id == candidate_id
        ]
        if not matches:
            raise CandidateNotFoundError(candidate_id)
        return matches[0]

    def _require_workflow(self, workflow_id: UUID):
        workflow = self._workflows.get(
            workflow_id,
            organization_id=self._organization_id,
            user_id=self._user_id,
        )
        if workflow is None:
            raise WorkflowNotFoundError(workflow_id)
        return workflow

    def _save(self, document: StoredSourceDocument, event: str) -> None:
        self._documents.save(
            document,
            user_id=self._user_id,
            actor_reference=self._actor_reference,
            event=event,
        )


def _source(document: StoredSourceDocument) -> SourceDocument:
    source = SourceDocument(
        id=document.id,
        filename=document.filename,
        document_type=document.document_type,
        source_reference=str(document.id),
        processing_status=document.processing_status,
        uploaded_at=document.uploaded_at,
        organization_reference=str(document.organization_id),
    )
    return source


def _replace(
    document: StoredSourceDocument,
    *,
    status: DocumentProcessingStatus,
    failure_code: str | None = None,
    pages=None,
    text_truncated: bool | None = None,
) -> StoredSourceDocument:
    return StoredSourceDocument(
        id=document.id,
        organization_id=document.organization_id,
        workflow_id=document.workflow_id,
        filename=document.filename,
        media_type=document.media_type,
        byte_size=document.byte_size,
        checksum_sha256=document.checksum_sha256,
        document_type=document.document_type,
        storage_key=document.storage_key,
        processing_status=status,
        failure_code=failure_code,
        text_truncated=(
            document.text_truncated if text_truncated is None else text_truncated
        ),
        pages=document.pages if pages is None else pages,
        uploaded_by_user_id=document.uploaded_by_user_id,
        uploaded_at=document.uploaded_at,
    )
