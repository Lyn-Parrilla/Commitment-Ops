"""Public file submission and the member download refusal."""

import hashlib
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.persistence import SessionFactory
from app.persistence.exceptions import PersistenceError
from app.persistence.orm import (
    AuditEventRecord,
    EvidenceFileRecord,
    EvidenceStorageCleanupRecord,
)
from tests.api.conftest import TEST_OTHER_ORGANIZATION_ID, TEST_OTHER_USER_ID
from tests.api.test_submission_api import (
    _evidence_count,
    _issue,
    _links,
    _requirement,
    _set_link,
    _token_from_path,
)
from tests.domain.test_evidence_files import _docx, _pdf, _png, _xlsx


def test_public_file_submission_stores_metadata_without_the_storage_key(
    api_client: TestClient,
    api_session_factory: SessionFactory,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    content = b'name,link\nAda,"=HYPERLINK(""https://example.test"")"\n'
    public = TestClient(api_client.app)

    response = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": token},
        data={"description": "Synthetic roster", "submitter_name": "Ada"},
        files={"file": ("roster.csv", content, "application/pdf")},
    )

    assert response.status_code == 201
    assert response.json() == {"status": "submitted"}
    visible = api_client.get(f"/api/v1/workflows/{workflow['id']}").json()
    saved = next(item for item in visible["requirements"] if item["id"] == requirement["id"])
    assert saved["status"] == requirement["status"]
    uploaded = saved["evidence"][0]
    assert uploaded["evidence_type"] == "document"
    assert uploaded["status"] == "submitted"
    assert uploaded["identity_verified"] is False
    assert uploaded["file"]["original_filename"] == "roster.csv"
    assert uploaded["file"]["media_type"] == "text/csv"
    assert uploaded["file"]["scan_status"] == "unscanned"
    assert uploaded["file"]["checksum_sha256"] == hashlib.sha256(content).hexdigest()
    assert "storage_key" not in visible_text(visible)
    assert "HYPERLINK" not in visible_text(visible)
    assert "=HYPERLINK" not in response.text
    stored = _stored_file(api_session_factory)
    assert stored.checksum_sha256 == uploaded["file"]["checksum_sha256"]
    assert stored.storage_key.count("/") == 2
    assert "roster" not in stored.storage_key
    assert api_client.evidence_storage.get(stored.storage_key) == content
    audit = _file_audit(api_session_factory)
    assert audit["checksum_sha256"] == stored.checksum_sha256
    assert "original_filename" not in audit
    assert "storage_key" not in audit
    assert token not in str(audit)


def test_each_allowed_file_type_can_be_submitted(api_client: TestClient) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    public = TestClient(api_client.app)
    samples = {
        "notes.pdf": _pdf(),
        "notes.docx": _docx(),
        "notes.xlsx": _xlsx(),
        "notes.csv": b"name\nAda\n",
        "notes.png": _png(),
        "notes.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 8,
    }

    for filename, content in samples.items():
        response = public.post(
            "/api/v1/submissions",
            headers={"X-Submission-Token": token},
            data={"description": filename},
            files={"file": (filename, content, "application/octet-stream")},
        )
        assert response.status_code == 201, filename

    visible = api_client.get(f"/api/v1/workflows/{workflow['id']}").json()
    saved = next(item for item in visible["requirements"] if item["id"] == requirement["id"])
    assert saved["status"] == requirement["status"]
    assert len(saved["evidence"]) == len(samples)
    assert {item["file"]["original_filename"] for item in saved["evidence"]} == set(samples)


def test_rejected_files_do_not_consume_a_use(
    api_client: TestClient,
    api_session_factory: SessionFactory,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    public = TestClient(api_client.app)
    rejected = [
        ("notes.pdf", b"x" * (10 * 1024 * 1024 + 1)),
        ("notes.pdf", _png()),
        ("notes.exe", _pdf()),
        ("notes.doc", _pdf()),
        ("notes.docx", bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 16),
    ]

    for filename, content in rejected:
        response = public.post(
            "/api/v1/submissions",
            headers={"X-Submission-Token": token},
            data={"description": "Synthetic file"},
            files={"file": (filename, content, "application/pdf")},
        )
        assert response.status_code == 422
        assert token not in response.text
    unknown = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": "not-a-real-token"},
        data={"description": "Synthetic file"},
        files={"file": ("notes.exe", _pdf(), "application/pdf")},
    )

    assert unknown.status_code == 422
    assert _links(api_session_factory)[0].use_count == 0
    assert api_client.evidence_storage._objects == {}
    assert _evidence_count(api_client, workflow["id"], requirement["id"]) == 0


def test_unusable_links_accept_no_file_and_leave_no_object(
    api_client: TestClient,
    api_session_factory: SessionFactory,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    public = TestClient(api_client.app)
    missing = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": "missing-token"},
        data={"description": "Synthetic file"},
        files={"file": ("notes.pdf", _pdf(), "application/pdf")},
    )
    _set_link(api_session_factory, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    expired = _post_file(public, token)
    _set_link(api_session_factory, expires_at=datetime.now(UTC) + timedelta(days=1))
    api_client.post(
        f"/api/v1/workflows/{workflow['id']}/requirements/{requirement['id']}/submission-links/{created['id']}/revoke"
    )
    revoked = _post_file(public, token)

    assert missing.status_code == 404
    assert expired.status_code == 404
    assert revoked.status_code == 404
    assert missing.json()["error"]["message"] == "This submission link is not valid."
    assert token not in expired.text
    assert api_client.evidence_storage._objects == {}
    assert _links(api_session_factory)[0].use_count == 0


def test_exhausted_and_cancelled_links_do_not_keep_a_file(
    api_client: TestClient,
    api_session_factory: SessionFactory,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    _set_link(api_session_factory, max_uses=1)
    public = TestClient(api_client.app)
    first = _post_file(public, token)
    second = _post_file(public, token)
    cancelled = api_client.post(
        "/api/v1/workflows",
        json={"name": "Cancelled file workflow", "workflow_type": "grant"},
    ).json()
    cancelled_requirement = api_client.post(
        f"/api/v1/workflows/{cancelled['id']}/requirements",
        json={
            "title": "Cancelled requirement",
            "description": "Synthetic",
            "owner": {"name": "Synthetic Owner", "email": "owner@example.org"},
            "evidence_expectation": "A record",
            "provenance": "user_provided",
        },
    ).json()
    issued = _issue(api_client, cancelled["id"], cancelled_requirement["id"])
    api_client.post(
        f"/api/v1/workflows/{cancelled['id']}/requirements/{cancelled_requirement['id']}/transitions",
        json={"action": "cancel"},
    )
    blocked = _post_file(public, _token_from_path(issued["submission_path"]))

    assert first.status_code == 201
    assert second.status_code == 404
    assert blocked.status_code == 404
    assert len(api_client.evidence_storage._objects) == 1


def test_member_download_is_refused_until_a_file_is_scanned(
    api_client: TestClient,
    token_factory,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    public = TestClient(api_client.app)
    _post_file(public, token)
    visible = api_client.get(f"/api/v1/workflows/{workflow['id']}").json()
    evidence_id = visible["requirements"][0]["evidence"][0]["id"]
    download = (
        f"/api/v1/workflows/{workflow['id']}/requirements/{requirement['id']}"
        f"/evidence/{evidence_id}/file"
    )

    owner = api_client.get(download)
    public_download = public.get(download, headers={"X-Submission-Token": token})
    foreign = api_client.get(
        download,
        headers={
            "Authorization": "Bearer "
            + token_factory(user_id=TEST_OTHER_USER_ID, email="other@example.test"),
            "X-Organization-ID": str(TEST_OTHER_ORGANIZATION_ID),
        },
    )

    assert owner.status_code == 409
    assert owner.json()["error"]["message"] == "This file is not available for download."
    assert b"%PDF" not in owner.content
    assert "storage_key" not in owner.text
    assert public_download.status_code == 401
    assert foreign.status_code == 404
    assert token not in owner.text


def test_database_failure_after_upload_does_not_keep_evidence(
    api_client: TestClient,
    api_session_factory: SessionFactory,
    monkeypatch,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])

    def fail(self, *args, **kwargs):
        raise PersistenceError(token)

    monkeypatch.setattr(
        "app.persistence.submissions.SubmissionRepository._commit_file",
        fail,
    )
    public = TestClient(api_client.app)
    response = _post_file(public, token)

    assert response.status_code == 503
    assert token not in response.text
    assert api_client.evidence_storage._objects == {}
    assert _evidence_count(api_client, workflow["id"], requirement["id"]) == 0
    assert _links(api_session_factory)[0].use_count == 0


def test_failed_cleanup_record_is_hidden_from_the_caller(
    api_client: TestClient,
    api_session_factory: SessionFactory,
    monkeypatch,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])

    def fail_commit(self, *args, **kwargs):
        raise PersistenceError("database failed")

    def fail_delete(key: str) -> None:
        raise PersistenceError("evidence object could not be deleted")

    monkeypatch.setattr(
        "app.persistence.submissions.SubmissionRepository._commit_file",
        fail_commit,
    )
    api_client.evidence_storage.delete = fail_delete
    public = TestClient(api_client.app)
    response = _post_file(public, token)

    with api_session_factory() as session:
        recorded_key = session.scalars(
            select(EvidenceStorageCleanupRecord.storage_key)
        ).one()

    assert response.status_code == 503
    assert (
        response.json()["error"]["message"]
        == "The accountability store could not complete the operation"
    )
    assert recorded_key not in response.text
    assert "storage_key" not in response.text
    assert token not in response.text
    assert _evidence_count(api_client, workflow["id"], requirement["id"]) == 0
    assert _links(api_session_factory)[0].use_count == 0


def _post_file(public: TestClient, token: str):
    return public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": token},
        data={"description": "Synthetic file"},
        files={"file": ("notes.pdf", _pdf(), "application/pdf")},
    )


def _stored_file(session_factory: SessionFactory) -> EvidenceFileRecord:
    with session_factory() as session:
        record = session.scalars(select(EvidenceFileRecord)).one()
        record.storage_key
        session.expunge(record)
        return record


def _file_audit(session_factory: SessionFactory) -> dict:
    with session_factory() as session:
        event = session.scalars(
            select(AuditEventRecord).where(AuditEventRecord.event_type == "evidence.added")
        ).one()
    assert isinstance(event.after_state, dict)
    return event.after_state


def visible_text(payload: dict) -> str:
    return str(payload)
