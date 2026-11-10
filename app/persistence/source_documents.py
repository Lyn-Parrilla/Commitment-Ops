"""Organization-scoped source documents. Object bytes are not stored here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.domain.discovery import DocumentProcessingStatus, DocumentType
from app.domain.document_text import ExtractedPage
from app.persistence.database import SessionFactory, raise_persistence_failure
from app.persistence.exceptions import PersistenceError
from app.persistence.orm import AuditEventRecord, SourceDocumentRecord
from app.persistence.rls import tenant_session

_FAILURE_CODES = frozenset(
    {
        "no_extractable_text",
        "interpretation_unavailable",
        "interpretation_invalid",
        "interpretation_failed",
    }
)


@dataclass(frozen=True)
class StoredSourceDocument:
    id: UUID
    organization_id: UUID
    workflow_id: UUID
    filename: str
    media_type: str
    byte_size: int
    checksum_sha256: str
    document_type: DocumentType
    storage_key: str
    processing_status: DocumentProcessingStatus
    failure_code: str | None
    text_truncated: bool
    pages: tuple[ExtractedPage, ...]
    uploaded_by_user_id: UUID
    uploaded_at: datetime


class SourceDocumentRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def find_duplicate(
        self,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        checksum_sha256: str,
        user_id: UUID,
    ) -> StoredSourceDocument | None:
        try:
            with tenant_session(
                self._session_factory,
                write=False,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                record = session.scalar(
                    select(SourceDocumentRecord).where(
                        SourceDocumentRecord.organization_id == organization_id,
                        SourceDocumentRecord.workflow_id == workflow_id,
                        SourceDocumentRecord.checksum_sha256 == checksum_sha256,
                    )
                )
                return None if record is None else _stored(record)
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("source document lookup failed", exc)

    def get(
        self,
        document_id: UUID,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        user_id: UUID,
    ) -> StoredSourceDocument | None:
        try:
            with tenant_session(
                self._session_factory,
                write=False,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                record = session.get(SourceDocumentRecord, document_id)
                if (
                    record is None
                    or record.organization_id != organization_id
                    or record.workflow_id != workflow_id
                ):
                    return None
                return _stored(record)
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("source document lookup failed", exc)

    def list_for_workflow(
        self,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        user_id: UUID,
    ) -> list[StoredSourceDocument]:
        try:
            with tenant_session(
                self._session_factory,
                write=False,
                user_id=user_id,
                organization_id=organization_id,
            ) as session:
                records = session.scalars(
                    select(SourceDocumentRecord)
                    .where(
                        SourceDocumentRecord.organization_id == organization_id,
                        SourceDocumentRecord.workflow_id == workflow_id,
                    )
                    .order_by(SourceDocumentRecord.uploaded_at, SourceDocumentRecord.id)
                )
                return [_stored(record) for record in records]
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("source document lookup failed", exc)

    def add(
        self,
        document: StoredSourceDocument,
        *,
        user_id: UUID,
        actor_reference: str | None,
    ) -> None:
        self._write(document, user_id=user_id, actor_reference=actor_reference, event="source_document_uploaded")

    def save(
        self,
        document: StoredSourceDocument,
        *,
        user_id: UUID,
        actor_reference: str | None,
        event: str,
    ) -> None:
        self._write(
            document,
            user_id=user_id,
            actor_reference=actor_reference,
            event=event,
        )

    def _write(
        self,
        document: StoredSourceDocument,
        *,
        user_id: UUID,
        actor_reference: str | None,
        event: str,
    ) -> None:
        if document.failure_code is not None and document.failure_code not in _FAILURE_CODES:
            raise PersistenceError("source document failure code is not valid")
        try:
            with tenant_session(
                self._session_factory,
                write=True,
                user_id=user_id,
                organization_id=document.organization_id,
            ) as session:
                record = session.get(SourceDocumentRecord, document.id)
                if record is None:
                    record = SourceDocumentRecord(
                        id=document.id,
                        organization_id=document.organization_id,
                        workflow_id=document.workflow_id,
                        uploaded_by_user_id=document.uploaded_by_user_id,
                        uploaded_at=document.uploaded_at,
                    )
                    session.add(record)
                elif (
                    record.organization_id != document.organization_id
                    or record.workflow_id != document.workflow_id
                    or record.checksum_sha256 != document.checksum_sha256
                    or record.storage_key != document.storage_key
                ):
                    raise PersistenceError(
                        "source document cannot change organization or storage identity"
                    )
                record.filename = document.filename
                record.media_type = document.media_type
                record.byte_size = document.byte_size
                record.checksum_sha256 = document.checksum_sha256
                record.document_type = document.document_type.value
                record.storage_key = document.storage_key
                record.processing_status = document.processing_status.value
                record.failure_code = document.failure_code
                record.text_truncated = document.text_truncated
                record.extracted_pages = [
                    {"page": page.page, "text": page.text} for page in document.pages
                ]
                session.add(
                    AuditEventRecord(
                        occurred_at=datetime.now(UTC),
                        entity_type="workflow",
                        entity_id=document.workflow_id,
                        event_type=event,
                        before_state=None,
                        after_state={
                            "document_id": str(document.id),
                            "status": document.processing_status.value,
                            "filename": document.filename,
                            "failure_code": document.failure_code,
                        },
                        actor_reference=actor_reference,
                        correlation_id=None,
                    )
                )
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise_persistence_failure("source document persistence failed", exc)


def _stored(record: SourceDocumentRecord) -> StoredSourceDocument:
    pages = []
    for item in record.extracted_pages or []:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        text = item.get("text")
        if isinstance(page, int) and page >= 1 and isinstance(text, str):
            pages.append(ExtractedPage(page=page, text=text))
    failure = record.failure_code if record.failure_code in _FAILURE_CODES else None
    return StoredSourceDocument(
        id=record.id,
        organization_id=record.organization_id,
        workflow_id=record.workflow_id,
        filename=record.filename,
        media_type=record.media_type,
        byte_size=record.byte_size,
        checksum_sha256=record.checksum_sha256,
        document_type=DocumentType(record.document_type),
        storage_key=record.storage_key,
        processing_status=DocumentProcessingStatus(record.processing_status),
        failure_code=failure,
        text_truncated=bool(record.text_truncated),
        pages=tuple(pages),
        uploaded_by_user_id=record.uploaded_by_user_id,
        uploaded_at=record.uploaded_at,
    )
