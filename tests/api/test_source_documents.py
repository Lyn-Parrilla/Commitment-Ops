"""Document intake stays inside the organization and behind a human decision."""

import io
import zipfile
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.document_interpretation import (
    DocumentInterpretationError,
    InterpretedCandidate,
)
from app.domain.discovery import DiscoveryClassification, SourceLocation
from app.domain.enums import RequirementProvenance
from app.persistence.database import SessionFactory
from app.persistence.exceptions import PersistenceError
from app.persistence.orm import AuditEventRecord
from tests.api.conftest import (
    TEST_ORGANIZATION_ID,
    TEST_OTHER_ORGANIZATION_ID,
    TEST_OTHER_USER_ID,
    TEST_USER_ID,
)


def _pdf(text: str) -> bytes:
    stream = f"BT ({text}) Tj ET".encode("latin-1")
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj\n"
        + f"4 0 obj << /Length {len(stream)} >> stream\n".encode()
        + stream
        + b"\nendstream endobj\ntrailer << /Root 1 0 R >>\n%%EOF"
    )


def _docx() -> bytes:
    document = b"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>The contractor shall deliver the report.</w:t></w:r></w:p></w:body>
</w:document>
"""
    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _workflow(api_client: TestClient) -> str:
    created = api_client.post(
        "/api/v1/workflows",
        json={"name": "Housing grant", "workflow_type": "grant"},
    )
    assert created.status_code == 201
    return created.json()["id"]


def _upload(api_client: TestClient, workflow_id: str, content: bytes, filename: str):
    return api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents",
        files={"file": (filename, content, "application/octet-stream")},
        data={"document_type": "grant_agreement"},
    )


def _candidate(title: str, provenance: RequirementProvenance, **overrides: object):
    values = dict(
        classification=DiscoveryClassification.REQUIREMENT,
        provenance=provenance,
        title=title,
        description=f"{title} is described in the agreement.",
        evidence_expectation="A filed report" if provenance == RequirementProvenance.DOCUMENT_EXPLICIT else None,
        due_date=None,
        target=None,
        explanation=f"{title} comes from the agreement text.",
        confidence=0.86 if provenance == RequirementProvenance.DOCUMENT_EXPLICIT else 0.35,
        uncertain=provenance == RequirementProvenance.AI_INFERRED,
        location=SourceLocation(page=1, section="Reports"),
    )
    values.update(overrides)
    return InterpretedCandidate(**values)


def _attacker_headers(token_factory) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token_factory(user_id=TEST_OTHER_USER_ID, email='other@example.test')}",
        "X-Organization-ID": str(TEST_OTHER_ORGANIZATION_ID),
    }


def test_unauthenticated_upload_is_rejected(
    api_client: TestClient,
) -> None:
    workflow_id = _workflow(api_client)
    sentence = "Private grant obligation."
    headers = dict(api_client.headers)
    api_client.headers.pop("Authorization", None)
    try:
        rejected = api_client.post(
            f"/api/v1/workflows/{workflow_id}/source-documents",
            files={"file": ("award.pdf", _pdf(sentence), "application/pdf")},
            data={"document_type": "grant_agreement"},
        )
    finally:
        api_client.headers.update(headers)

    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "authentication_required"
    assert "storage_key" not in rejected.text
    assert "checksum" not in rejected.text
    assert sentence not in rejected.text


def test_upload_process_review_and_human_addition(
    api_client: TestClient,
    api_session_factory: SessionFactory,
    token_factory,
) -> None:
    workflow_id = _workflow(api_client)
    sentence = "The grantee shall submit a quarterly report."
    uploaded = _upload(api_client, workflow_id, _pdf(sentence), "award.pdf")
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["duplicate"] is False
    assert body["document"]["processing_status"] == "uploaded"
    assert "storage_key" not in uploaded.text
    assert "checksum" not in uploaded.text
    assert sentence not in uploaded.text
    document_id = body["document"]["id"]

    duplicate = _upload(api_client, workflow_id, _pdf(sentence), "award.pdf")
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["document"]["id"] == document_id
    assert len(api_client.source_storage._objects) == 1

    api_client.interpreter.results = [
        _candidate("Quarterly report", RequirementProvenance.DOCUMENT_EXPLICIT),
        _candidate("Continuing report", RequirementProvenance.AI_INFERRED),
    ]
    processed = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/process"
    )
    assert processed.status_code == 200
    assert processed.json()["processing_status"] == "processed"
    assert processed.json()["failure_code"] is None
    assert sentence not in processed.text
    again = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/process"
    )
    assert again.status_code == 200
    assert api_client.interpreter.calls == 1

    excerpt = api_client.get(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/excerpt",
        params={"page": 1},
    )
    assert excerpt.status_code == 200
    assert excerpt.json()["page"] == 1
    assert sentence in excerpt.json()["text"]
    assert "http" not in excerpt.json()["text"]

    listed = api_client.get(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/candidates"
    )
    assert listed.status_code == 200
    candidates = listed.json()
    by_title = {item["title"]: item for item in candidates}
    explicit = by_title["Quarterly report"]
    inferred = by_title["Continuing report"]
    assert explicit["provenance"] == "document_explicit"
    assert inferred["provenance"] == "ai_inferred"
    assert explicit["is_authoritative"] is False
    assert inferred["is_authoritative"] is False
    assert explicit["source_location"]["page"] == 1
    assert explicit["source_location"]["section"] == "Reports"
    assert "owner" in explicit["missing_fields"]
    assert inferred["discovery_reason"].startswith("Uncertain.")
    assert explicit["confidence"] == 0.86

    workflow = api_client.get(f"/api/v1/workflows/{workflow_id}").json()
    assert workflow["requirements"] == []

    premature = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/candidates/{explicit['id']}/review",
        json={"action": "approve"},
    )
    assert premature.status_code == 422
    assert api_client.get(f"/api/v1/workflows/{workflow_id}").json()["requirements"] == []

    forged = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/candidates/{explicit['id']}/review",
        json={
            "action": "approve",
            "reviewer_id": str(TEST_OTHER_USER_ID),
        },
    )
    assert forged.status_code == 422

    revised = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/candidates/{explicit['id']}/revisions",
        json={
            "owner": {"name": "Ada Lovelace", "email": "ada@example.test"},
            "evidence_expectation": "The filed quarterly report",
        },
    )
    assert revised.status_code == 200
    assert revised.json()["review_status"] == "pending_review"
    assert revised.json()["is_authoritative"] is False

    approved = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/candidates/{explicit['id']}/review",
        json={"action": "approve", "note": "This obligation is explicit."},
    )
    assert approved.status_code == 200
    assert approved.json()["review_status"] == "approved"
    assert approved.json()["is_authoritative"] is False
    assert approved.json()["reviewed_by"]["id"] == str(TEST_USER_ID)
    assert approved.json()["reviewed_by"]["email"] == "reviewer@example.test"

    rejected = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/candidates/{inferred['id']}/review",
        json={"action": "reject", "reason": "Background only."},
    )
    assert rejected.status_code == 200
    assert rejected.json()["review_status"] == "rejected"
    assert rejected.json()["is_authoritative"] is False

    added = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/requirements",
        json={
            "title": "Site visit",
            "description": "Host the annual site visit.",
            "owner": {"name": "Grace Hopper", "email": "grace@example.test"},
            "evidence_expectation": "The visit notes",
            "source_location": {"page": 1, "section": "Reports"},
            "reason": "The reading missed the visit.",
        },
    )
    assert added.status_code == 201
    assert added.json()["provenance"] == "user_provided"
    assert added.json()["is_authoritative"] is True
    assert added.json()["reviewed_by"] is None or added.json()["review_status"] == "approved"

    visible = api_client.get(f"/api/v1/workflows/{workflow_id}").json()
    titles = {item["title"]: item for item in visible["requirements"]}
    assert set(titles) == {"Quarterly report", "Site visit"}
    assert titles["Quarterly report"]["is_authoritative"] is True
    assert titles["Quarterly report"]["provenance"] == "document_explicit"
    assert titles["Site visit"]["provenance"] == "user_provided"
    assert "Continuing report" not in titles

    attacker = _attacker_headers(token_factory)
    hidden = api_client.get(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}",
        headers=attacker,
    )
    assert hidden.status_code == 404
    hidden_candidates = api_client.get(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/candidates",
        headers=attacker,
    )
    assert hidden_candidates.status_code == 404
    denied = api_client.get(
        f"/api/v1/workflows/{workflow_id}/source-documents",
        headers={
            "Authorization": f"Bearer {token_factory()}",
            "X-Organization-ID": str(TEST_OTHER_ORGANIZATION_ID),
        },
    )
    assert denied.status_code == 403

    with api_session_factory() as session:
        events = list(
            session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.entity_id == UUID(workflow_id)
                )
            )
        )
    event_types = {event.event_type for event in events}
    assert "source_document_uploaded" in event_types
    assert "source_document_processed" in event_types
    assert "source_document_reviewed" in event_types
    assert all(event.actor_reference == str(TEST_USER_ID) for event in events if event.event_type.startswith("source_document_"))
    serialized = " ".join(str(event.after_state) for event in events)
    assert sentence not in serialized
    assert "storage_key" not in serialized

    script = api_client.get("/assets/app.js").text
    assert "source-documents" in script
    assert "apiForm" in script
    assert "Explicit obligation" in script
    assert "Inferred obligation" in script
    assert 'provenance: "ai_inferred"' not in script
    assert "storage_key" not in script


def test_invalid_oversized_and_failed_processing(api_client: TestClient) -> None:
    workflow_id = _workflow(api_client)
    rejected_type = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents",
        files={"file": ("notes.csv", b"column\nvalue\n", "text/csv")},
        data={"document_type": "grant_agreement"},
    )
    assert rejected_type.status_code == 422
    assert "column" not in rejected_type.text

    oversized = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents",
        files={
            "file": (
                "huge.pdf",
                b"%PDF-" + (b"0" * (10 * 1024 * 1024)),
                "application/pdf",
            )
        },
        data={"document_type": "contract"},
    )
    assert oversized.status_code == 422
    assert oversized.json()["error"]["message"] == "This file is too large."

    uploaded = _upload(api_client, workflow_id, _pdf("Submit the report."), "award.pdf")
    assert uploaded.status_code == 201
    document_id = uploaded.json()["document"]["id"]
    api_client.interpreter.error = DocumentInterpretationError("interpretation_failed")
    failed = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/process"
    )
    assert failed.status_code == 200
    assert failed.json()["processing_status"] == "failed"
    assert failed.json()["failure_code"] == "interpretation_failed"
    assert api_client.get(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/candidates"
    ).json() == []

    api_client.interpreter.error = None
    api_client.interpreter.results = [
        _candidate("Filed report", RequirementProvenance.DOCUMENT_EXPLICIT)
    ]
    recovered = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/process"
    )
    assert recovered.status_code == 200
    assert recovered.json()["processing_status"] == "processed"
    assert len(api_client.get(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/candidates"
    ).json()) == 1

    empty = _upload(api_client, workflow_id, b"%PDF-1.4\n%%EOF", "blank.pdf")
    assert empty.status_code == 201
    blank = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{empty.json()['document']['id']}/process"
    )
    assert blank.status_code == 200
    assert blank.json()["failure_code"] == "no_extractable_text"

    word = _upload(api_client, workflow_id, _docx(), "agreement.docx")
    assert word.status_code == 201
    api_client.interpreter.results = []
    read = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{word.json()['document']['id']}/process"
    )
    assert read.status_code == 200
    assert read.json()["processing_status"] == "processed"
    assert api_client.interpreter.pages[0].text == "The contractor shall deliver the report."


def test_storage_failure_does_not_reveal_the_document(
    api_client: TestClient,
) -> None:
    workflow_id = _workflow(api_client)
    uploaded = _upload(api_client, workflow_id, _pdf("Private obligation."), "award.pdf")
    document_id = uploaded.json()["document"]["id"]

    def unavailable(key: str) -> bytes:
        raise PersistenceError("private-document-bytes")

    api_client.source_storage.get = unavailable
    failed = api_client.post(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}/process"
    )
    assert failed.status_code == 503
    assert "private-document-bytes" not in failed.text
    assert "Private obligation" not in failed.text
    stored = api_client.get(
        f"/api/v1/workflows/{workflow_id}/source-documents/{document_id}"
    )
    assert stored.status_code == 200
    assert stored.json()["processing_status"] == "failed"
    assert stored.json()["failure_code"] == "interpretation_failed"
