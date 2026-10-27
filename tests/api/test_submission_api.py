import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select
from app.domain import Evidence, EvidenceType, RequirementStatus, WorkflowStatus
from app.persistence import SessionFactory, WorkflowRepository
from app.persistence.orm import AuditEventRecord, SubmissionLinkRecord
from tests.api.conftest import (
    TEST_ORGANIZATION_ID,
    TEST_OTHER_ORGANIZATION_ID,
    TEST_OTHER_USER_ID,
    TEST_USER_ID,
)

ORGANIZATION_ID = TEST_ORGANIZATION_ID


def test_member_can_issue_and_revoke_a_submission_link(
    api_client: TestClient,
    api_session_factory: SessionFactory,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    raw_token = _token_from_path(created["submission_path"])

    assert created["max_uses"] == 25
    assert created["use_count"] == 0
    assert created["revoked_at"] is None
    stored = _links(api_session_factory)
    assert len(stored) == 1
    assert stored[0].token_hash != raw_token
    assert raw_token not in json.dumps(
        {
            "hash": stored[0].token_hash,
            "creator": str(stored[0].created_by_user_id),
        }
    )

    listed = api_client.get(_links_url(workflow["id"], requirement["id"]))
    assert listed.status_code == 200
    assert "submission_path" not in listed.text
    assert raw_token not in listed.text

    revoked = api_client.post(
        f"{_links_url(workflow['id'], requirement['id'])}/{created['id']}/revoke"
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert raw_token not in revoked.text


def test_public_submission_appends_unverified_evidence_and_a_minimal_audit_event(
    api_client: TestClient,
    api_repository: WorkflowRepository,
    api_session_factory: SessionFactory,
) -> None:
    workflow, requirement = _requirement(api_client)
    _attach_member_evidence(api_repository, UUID(workflow["id"]))
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    other = _add_requirement(api_client, workflow["id"], title="Unrelated requirement")
    public = TestClient(api_client.app)
    before = datetime.now(UTC)

    context = public.get(
        "/api/v1/submission-context",
        headers={"X-Submission-Token": token},
    )
    first = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": token, "X-Organization-ID": str(TEST_OTHER_ORGANIZATION_ID)},
        json={
            "description": "Synthetic quarterly receipt",
            "reference": "https://example.test/receipts/q3",
            "submitter_name": "Synthetic Submitter",
        },
    )
    second = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": token},
        json={
            "description": "Synthetic follow-up receipt",
            "reference": "https://example.test/receipts/q3-follow-up",
        },
    )

    assert context.status_code == 200
    assert context.json() == {
        "organization_name": "Synthetic Organization",
        "requirement_title": requirement["title"],
        "evidence_expectation": requirement["evidence_expectation"],
    }
    assert other["title"] not in context.text
    assert "Member synthetic note" not in context.text
    assert first.status_code == 201
    assert first.json() == {"status": "submitted"}
    assert second.status_code == 201
    visible = api_client.get(f"/api/v1/workflows/{workflow['id']}").json()
    saved = next(item for item in visible["requirements"] if item["id"] == requirement["id"])
    assert saved["status"] == requirement["status"]
    assert saved["review_status"] == requirement["review_status"]
    public_rows = {
        item["description"]: item
        for item in saved["evidence"]
        if item["identity_verified"] is False
    }
    assert set(public_rows) == {
        "Synthetic quarterly receipt",
        "Synthetic follow-up receipt",
    }
    quarterly = public_rows["Synthetic quarterly receipt"]
    follow_up = public_rows["Synthetic follow-up receipt"]
    assert quarterly["id"] != follow_up["id"]
    assert quarterly["submitted_by"] is None
    assert quarterly["submitter_name"] == "Synthetic Submitter"
    assert follow_up["submitter_name"] is None
    assert quarterly["evidence_type"] == "link"
    assert quarterly["status"] == "submitted"
    submitted_at = datetime.fromisoformat(quarterly["submitted_at"])
    assert before - timedelta(minutes=1) <= submitted_at <= datetime.now(UTC) + timedelta(minutes=1)
    member_rows = [item for item in saved["evidence"] if item["identity_verified"] is True]
    assert len(member_rows) == 1
    assert member_rows[0]["submitted_by"]["email"] == "owner@example.org"
    unrelated = next(item for item in visible["requirements"] if item["id"] == other["id"])
    assert unrelated["evidence"] == []

    events = api_repository.list_audit_events(
        UUID(workflow["id"]),
        organization_id=ORGANIZATION_ID,
        user_id=TEST_USER_ID,
    )
    added = [event for event in events if event.event_type == "evidence.added"]
    public_events = [
        event
        for event in added
        if event.actor_reference
        and event.actor_reference.startswith("submission-link:")
    ]
    assert len(public_events) == 2
    for event in public_events:
        encoded = json.dumps(
            {
                "actor": event.actor_reference,
                "correlation": event.correlation_id,
                "after": event.after_state,
            }
        )
        assert token not in encoded
        assert "https://example.test" not in encoded
        assert event.after_state["organization_id"] == str(ORGANIZATION_ID)
        assert event.after_state["workflow_id"] == workflow["id"]
        assert event.after_state["requirement_id"] == requirement["id"]
        assert event.after_state["status"] == "submitted"
        assert event.actor_reference == f"submission-link:{created['id']}"
    with api_session_factory() as session:
        audit_text = json.dumps(
            [
                {
                    "actor": row.actor_reference,
                    "after": row.after_state,
                }
                for row in session.scalars(select(AuditEventRecord)).all()
            ]
        )
    assert token not in audit_text


def test_invalid_expired_revoked_and_exhausted_links_fail_generically(
    api_client: TestClient,
    api_session_factory: SessionFactory,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    public = TestClient(api_client.app)
    missing = public.post("/api/v1/submissions", json=_payload())
    unknown = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": "not-a-real-submission-token"},
        json=_payload(),
    )
    assert missing.status_code == unknown.status_code == 404
    assert missing.json() == unknown.json()
    assert token not in missing.text

    _set_link(api_session_factory, max_uses=1)
    accepted = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": token},
        json=_payload("First synthetic receipt"),
    )
    exhausted = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": token},
        json=_payload("Second synthetic receipt"),
    )
    assert accepted.status_code == 201
    assert exhausted.status_code == 404
    assert exhausted.json()["error"]["message"] == missing.json()["error"]["message"]
    assert _evidence_count(api_client, workflow["id"], requirement["id"]) == 1

    _set_link(api_session_factory, max_uses=2, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    expired = public.get(
        "/api/v1/submission-context",
        headers={"X-Submission-Token": token},
    )
    assert expired.status_code == 404
    assert expired.json()["error"]["message"] == missing.json()["error"]["message"]

    fresh = _issue(api_client, workflow["id"], requirement["id"])
    fresh_token = _token_from_path(fresh["submission_path"])
    api_client.post(
        f"{_links_url(workflow['id'], requirement['id'])}/{fresh['id']}/revoke"
    )
    revoked = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": fresh_token},
        json=_payload("Revoked synthetic receipt"),
    )
    assert revoked.status_code == 404
    assert revoked.json()["error"]["message"] == missing.json()["error"]["message"]
    assert fresh_token not in revoked.text


def test_bad_submission_payload_does_not_consume_a_use(
    api_client: TestClient,
    api_session_factory: SessionFactory,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    public = TestClient(api_client.app)

    rejected = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": token},
        json={
            "description": "Synthetic receipt",
            "reference": "http://example.test/insecure",
            "submitted_at": "2000-01-01T00:00:00Z",
            "requirement_id": requirement["id"],
        },
    )
    insecure = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": "unknown-token"},
        json={
            "description": "Synthetic receipt",
            "reference": "http://example.test/insecure",
        },
    )

    assert rejected.status_code == 422
    assert insecure.status_code == 422
    assert _links(api_session_factory)[0].use_count == 0
    assert _evidence_count(api_client, workflow["id"], requirement["id"]) == 0


def test_cancelled_workflow_and_requirement_reject_submission_links(
    api_client: TestClient,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    cancel_requirement = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/requirements/{requirement['id']}/transitions",
        json={"action": "cancel"},
    )
    assert cancel_requirement.status_code == 200
    public = TestClient(api_client.app)
    rejected = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": token},
        json=_payload(),
    )
    assert rejected.status_code == 404
    assert _evidence_count(api_client, workflow["id"], requirement["id"]) == 0
    blocked = api_client.post(_links_url(workflow["id"], requirement["id"]))
    assert blocked.status_code == 409

    open_workflow, _open_requirement = _requirement(api_client)
    cancelled = api_client.post(
        f"/api/v1/workflows/{open_workflow['id']}/transitions",
        json={"action": "cancel"},
    )
    assert cancelled.status_code == 200
    link = api_client.post(_links_url(open_workflow["id"], _open_requirement["id"]))
    assert link.status_code == 409


def test_completed_workflow_rejects_submission_links(api_client: TestClient) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    activated = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/transitions",
        json={"action": "activate"},
    )
    completed = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/transitions",
        json={"action": "complete"},
    )
    public = TestClient(api_client.app)
    rejected = public.post(
        "/api/v1/submissions",
        headers={"X-Submission-Token": token},
        json=_payload(),
    )
    blocked = api_client.post(_links_url(workflow["id"], requirement["id"]))

    assert activated.status_code == 200
    assert completed.status_code == 200
    assert rejected.status_code == 404
    assert rejected.json()["error"]["message"] == "This submission link is not valid."
    assert token not in rejected.text
    assert _evidence_count(api_client, workflow["id"], requirement["id"]) == 0
    assert blocked.status_code == 409


def test_public_token_cannot_list_workflows_or_cross_organizations(
    api_client: TestClient,
    token_factory,
) -> None:
    workflow, requirement = _requirement(api_client)
    created = _issue(api_client, workflow["id"], requirement["id"])
    token = _token_from_path(created["submission_path"])
    public = TestClient(api_client.app)

    workflows = public.get(
        "/api/v1/workflows",
        headers={"X-Submission-Token": token},
    )
    detail = public.get(
        f"/api/v1/workflows/{workflow['id']}",
        headers={"X-Submission-Token": token},
    )
    transition = public.post(
        f"/api/v1/workflows/{workflow['id']}/requirements/{requirement['id']}/transitions",
        headers={"X-Submission-Token": token},
        json={"action": "complete"},
    )
    foreign = api_client.post(
        _links_url(workflow["id"], requirement["id"]),
        headers={
            "Authorization": (
                "Bearer "
                + token_factory(
                    user_id=TEST_OTHER_USER_ID,
                    email="other@example.test",
                )
            ),
            "X-Organization-ID": str(TEST_OTHER_ORGANIZATION_ID),
        },
    )

    assert workflows.status_code == 401
    assert detail.status_code == 401
    assert transition.status_code == 401
    assert foreign.status_code == 404
    assert token not in workflows.text
    assert token not in detail.text


def _requirement(api_client: TestClient) -> tuple[dict, dict]:
    workflow = api_client.post(
        "/api/v1/workflows",
        json={"name": "Synthetic evidence workflow", "workflow_type": "grant"},
    )
    assert workflow.status_code == 201
    requirement = _add_requirement(api_client, workflow.json()["id"])
    return workflow.json(), requirement


def _add_requirement(
    api_client: TestClient,
    workflow_id: str,
    *,
    title: str = "Submit quarterly outcomes report",
) -> dict:
    response = api_client.post(
        f"/api/v1/workflows/{workflow_id}/requirements",
        json={
            "title": title,
            "description": "Submit the synthetic quarterly report.",
            "owner": {
                "name": "Synthetic Owner",
                "email": "owner@example.org",
            },
            "evidence_expectation": "Signed participant records",
            "provenance": "user_provided",
        },
    )
    assert response.status_code == 201
    return response.json()


def _issue(api_client: TestClient, workflow_id: str, requirement_id: str) -> dict:
    response = api_client.post(_links_url(workflow_id, requirement_id))
    assert response.status_code == 201
    return response.json()


def _links_url(workflow_id: str, requirement_id: str) -> str:
    return (
        f"/api/v1/workflows/{workflow_id}/requirements/"
        f"{requirement_id}/submission-links"
    )


def _token_from_path(path: str) -> str:
    prefix = "#/s/"
    assert path.startswith(prefix)
    return path[len(prefix) :]


def _payload(description: str = "Synthetic receipt") -> dict:
    return {
        "description": description,
        "reference": "https://example.test/receipt",
    }


def _links(session_factory: SessionFactory) -> list[SubmissionLinkRecord]:
    with session_factory() as session:
        return list(session.scalars(select(SubmissionLinkRecord)).all())


def _set_link(
    session_factory: SessionFactory,
    *,
    max_uses: int | None = None,
    expires_at: datetime | None = None,
) -> None:
    with session_factory() as session:
        link = session.scalars(select(SubmissionLinkRecord)).one()
        if max_uses is not None:
            link.max_uses = max_uses
        if expires_at is not None:
            link.expires_at = expires_at
        session.commit()


def _evidence_count(api_client: TestClient, workflow_id: str, requirement_id: str) -> int:
    workflow = api_client.get(f"/api/v1/workflows/{workflow_id}").json()
    requirement = next(
        item for item in workflow["requirements"] if item["id"] == requirement_id
    )
    return len(requirement["evidence"])


def _attach_member_evidence(
    repository: WorkflowRepository,
    workflow_id: UUID,
) -> None:
    workflow = repository.get(
        workflow_id,
        organization_id=ORGANIZATION_ID,
        user_id=TEST_USER_ID,
    )
    assert workflow is not None
    requirement = workflow.requirements[0]
    requirement.add_evidence(
        Evidence.create(
            description="Member synthetic note",
            evidence_type=EvidenceType.NOTE,
            submitted_by=requirement.owner,
        )
    )
    repository.save(
        workflow,
        user_id=TEST_USER_ID,
        organization_id=ORGANIZATION_ID,
        actor_reference=str(TEST_USER_ID),
    )
    assert requirement.status == RequirementStatus.NOT_STARTED
    assert workflow.status == WorkflowStatus.DRAFT
