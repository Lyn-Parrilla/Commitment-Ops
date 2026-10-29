"""Tenant A cannot read or change tenant B's evidence through the API."""

from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.persistence import SessionFactory
from app.persistence.exceptions import PersistenceError
from app.persistence.orm import EvidenceFileRecord, EvidenceRecord
from tests.api.conftest import TEST_OTHER_ORGANIZATION_ID, TEST_OTHER_USER_ID
from tests.api.test_evidence_uploads import _stored_file
from tests.api.test_submission_api import _issue, _requirement, _token_from_path

_VICTIM_BYTES = b"%PDF-1.4\nvictim-synthetic\n"
_ATTACKER_BYTES = b"%PDF-1.4\nattacker-synthetic\n"


def test_other_tenant_cannot_read_evidence_through_the_application(
    api_client: TestClient,
    api_session_factory: SessionFactory,
    token_factory,
) -> None:
    workflow, requirement, download = _victim_file(api_client)
    stored = _stored_file(api_session_factory)
    attacker = _attacker_headers(token_factory)
    reads, deletes = _guard_storage(api_client, stored.storage_key)

    denied = api_client.get(download, headers=attacker)
    hidden_workflow = api_client.get(
        f"/api/v1/workflows/{workflow['id']}",
        headers=attacker,
    )
    hidden_list = api_client.get("/api/v1/workflows", headers=attacker)
    anonymous = TestClient(api_client.app).get(download)
    owner = api_client.get(download)
    visible = api_client.get(f"/api/v1/workflows/{workflow['id']}").json()

    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "not_found"
    assert _VICTIM_BYTES not in denied.content
    assert stored.storage_key not in denied.text
    assert hidden_workflow.status_code == 404
    assert hidden_list.status_code == 200
    assert hidden_list.json() == []
    assert anonymous.status_code == 401
    assert owner.status_code == 409
    assert owner.json()["error"]["message"] == "This file is not available for download."
    assert _VICTIM_BYTES not in owner.content
    assert stored.storage_key not in owner.text
    uploaded = visible["requirements"][0]["evidence"][0]["file"]
    assert uploaded["original_filename"] == "notes.pdf"
    assert uploaded["scan_status"] == "unscanned"
    assert "storage_key" not in str(visible)
    assert reads == []
    assert deletes == []
    assert api_client.evidence_storage._objects[stored.storage_key].body == _VICTIM_BYTES
    assert _stored_file_for_requirement(
        api_session_factory, requirement["id"]
    ).storage_key == stored.storage_key


def test_other_tenant_cannot_overwrite_or_delete_the_object(
    api_client: TestClient,
    api_session_factory: SessionFactory,
    token_factory,
) -> None:
    workflow, requirement, download = _victim_file(api_client)
    stored = _stored_file(api_session_factory)
    attacker_headers = _attacker_headers(token_factory)
    attacker_workflow = api_client.post(
        "/api/v1/workflows",
        headers=attacker_headers,
        json={"name": "Attacker workflow", "workflow_type": "grant"},
    )
    assert attacker_workflow.status_code == 201
    attacker_requirement = api_client.post(
        f"/api/v1/workflows/{attacker_workflow.json()['id']}/requirements",
        headers=attacker_headers,
        json={
            "title": "Attacker requirement",
            "description": "Synthetic",
            "owner": {
                "name": "Attacker Owner",
                "email": "attacker-owner@example.org",
            },
            "evidence_expectation": "A synthetic record",
            "provenance": "user_provided",
        },
    )
    assert attacker_requirement.status_code == 201
    issued = api_client.post(
        f"/api/v1/workflows/{attacker_workflow.json()['id']}/requirements/"
        f"{attacker_requirement.json()['id']}/submission-links",
        headers=attacker_headers,
    )
    assert issued.status_code == 201
    public = TestClient(api_client.app)
    rejected_key = public.post(
        "/api/v1/submissions",
        headers={
            "X-Submission-Token": _token_from_path(
                issued.json()["submission_path"]
            )
        },
        data={
            "description": "Attacker file",
            "storage_key": stored.storage_key,
        },
        files={"file": ("notes.pdf", _ATTACKER_BYTES, "application/pdf")},
    )
    submitted = public.post(
        "/api/v1/submissions",
        headers={
            "X-Submission-Token": _token_from_path(
                issued.json()["submission_path"]
            )
        },
        data={"description": "Attacker file"},
        files={"file": ("notes.pdf", _ATTACKER_BYTES, "application/pdf")},
    )

    assert rejected_key.status_code == 422
    assert _VICTIM_BYTES not in rejected_key.content
    assert stored.storage_key not in rejected_key.text
    assert submitted.status_code == 201
    for method in ("delete", "put", "patch"):
        response = getattr(api_client, method)(download, headers=attacker_headers)
        assert response.status_code == 405
        assert _VICTIM_BYTES not in response.content
    assert api_client.evidence_storage._objects[stored.storage_key].body == _VICTIM_BYTES
    attacker_keys = [
        key
        for key, item in api_client.evidence_storage._objects.items()
        if item.body == _ATTACKER_BYTES
    ]
    assert len(attacker_keys) == 1
    assert attacker_keys[0] != stored.storage_key
    victim_row = _stored_file_for_requirement(api_session_factory, requirement["id"])
    assert victim_row.storage_key == stored.storage_key
    owner = api_client.get(download)
    assert owner.status_code == 409
    assert _VICTIM_BYTES not in owner.content


def test_other_tenant_failure_and_cleanup_leave_the_victim_object(
    api_client: TestClient,
    api_session_factory: SessionFactory,
    token_factory,
    monkeypatch,
) -> None:
    _workflow, _requirement, _download = _victim_file(api_client)
    stored = _stored_file(api_session_factory)
    attacker_headers = _attacker_headers(token_factory)
    attacker_workflow = api_client.post(
        "/api/v1/workflows",
        headers=attacker_headers,
        json={"name": "Attacker failure workflow", "workflow_type": "grant"},
    )
    assert attacker_workflow.status_code == 201
    attacker_requirement = api_client.post(
        f"/api/v1/workflows/{attacker_workflow.json()['id']}/requirements",
        headers=attacker_headers,
        json={
            "title": "Attacker failure requirement",
            "description": "Synthetic",
            "owner": {
                "name": "Attacker Owner",
                "email": "attacker-failure@example.org",
            },
            "evidence_expectation": "A synthetic record",
            "provenance": "user_provided",
        },
    )
    assert attacker_requirement.status_code == 201
    issued = api_client.post(
        f"/api/v1/workflows/{attacker_workflow.json()['id']}/requirements/"
        f"{attacker_requirement.json()['id']}/submission-links",
        headers=attacker_headers,
    )
    assert issued.status_code == 201

    def fail(self, *args, **kwargs):
        raise PersistenceError("database failed")

    monkeypatch.setattr(
        "app.persistence.submissions.SubmissionRepository._commit_file",
        fail,
    )
    original_delete = api_client.evidence_storage.delete

    def guarded_delete(key: str) -> None:
        if key == stored.storage_key:
            raise AssertionError("victim object was deleted")
        original_delete(key)

    api_client.evidence_storage.delete = guarded_delete
    response = TestClient(api_client.app).post(
        "/api/v1/submissions",
        headers={
            "X-Submission-Token": _token_from_path(issued.json()["submission_path"])
        },
        data={"description": "Attacker file"},
        files={"file": ("notes.pdf", _ATTACKER_BYTES, "application/pdf")},
    )

    assert response.status_code == 503
    assert _VICTIM_BYTES not in response.content
    assert stored.storage_key not in response.text
    assert api_client.evidence_storage._objects[stored.storage_key].body == _VICTIM_BYTES
    assert all(
        item.body != _ATTACKER_BYTES
        for item in api_client.evidence_storage._objects.values()
    )
    with api_session_factory() as session:
        cleanup_keys = session.scalars(
            select(EvidenceFileRecord.storage_key)
        ).all()
    assert cleanup_keys == [stored.storage_key]


def _victim_file(api_client: TestClient) -> tuple[dict, dict, str]:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    response = TestClient(api_client.app).post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": _token_from_path(created["submission_path"])},
        data={"description": "Victim file"},
        files={"file": ("notes.pdf", _VICTIM_BYTES, "application/pdf")},
    )
    assert response.status_code == 201
    download = (
        f"/api/v1/workflows/{workflow['id']}/requirements/{requirement['id']}"
        f"/evidence/{_evidence_identifier(api_client, workflow['id'])}/file"
    )
    return workflow, requirement, download


def _evidence_identifier(api_client: TestClient, workflow_id: str) -> str:
    visible = api_client.get(f"/api/v1/workflows/{workflow_id}").json()
    return visible["requirements"][0]["evidence"][0]["id"]


def _attacker_headers(token_factory) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + token_factory(user_id=TEST_OTHER_USER_ID, email="attacker@example.test"),
        "X-Organization-ID": str(TEST_OTHER_ORGANIZATION_ID),
    }


def _guard_storage(api_client: TestClient, victim_key: str) -> tuple[list[str], list[str]]:
    reads: list[str] = []
    deletes: list[str] = []
    storage = api_client.evidence_storage
    original_get = storage.get
    original_delete = storage.delete

    def guarded_get(key: str) -> bytes:
        reads.append(key)
        return original_get(key)

    def guarded_delete(key: str) -> None:
        deletes.append(key)
        if key == victim_key:
            raise AssertionError("victim object was deleted")
        original_delete(key)

    storage.get = guarded_get
    storage.delete = guarded_delete
    return reads, deletes


def _stored_file_for_requirement(
    api_session_factory: SessionFactory,
    requirement_id: str,
) -> EvidenceFileRecord:
    with api_session_factory() as session:
        record = session.scalars(
            select(EvidenceFileRecord)
            .join(EvidenceRecord, EvidenceRecord.id == EvidenceFileRecord.evidence_id)
            .where(EvidenceRecord.requirement_id == UUID(requirement_id))
        ).one()
        record.storage_key
        record.checksum_sha256
        session.expunge(record)
        return record
