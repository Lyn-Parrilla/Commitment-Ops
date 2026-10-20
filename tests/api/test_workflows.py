from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.domain import RequirementStatus, ReviewStatus, WorkflowStatus
from app.persistence import WorkflowRepository
from tests.api.conftest import TEST_OTHER_ORGANIZATION_ID, TEST_OTHER_USER_ID

ORGANIZATION_ID = UUID("30000000-0000-0000-0000-000000000001")


def test_create_and_retrieve_workflow(api_client: TestClient) -> None:
    created = _create_workflow(
        api_client,
        name="Synthetic Grant Accountability",
        workflow_type="grant",
    )

    response = api_client.get(f"/api/v1/workflows/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created
    assert created["status"] == "draft"
    assert created["requirements"] == []


def test_list_workflows_supports_status_and_type_filters(
    api_client: TestClient,
) -> None:
    grant = _create_workflow(api_client, name="Active grant", workflow_type="grant")
    contract = _create_workflow(
        api_client,
        name="Draft contract",
        workflow_type="vendor_contract",
    )
    activation = api_client.post(
        f"/api/v1/workflows/{grant['id']}/transitions",
        json={"action": "activate"},
    )
    assert activation.status_code == 200

    by_status = api_client.get("/api/v1/workflows", params={"status": "active"})
    by_type = api_client.get(
        "/api/v1/workflows",
        params={"workflow_type": "vendor_contract"},
    )

    assert [item["id"] for item in by_status.json()] == [grant["id"]]
    assert [item["id"] for item in by_type.json()] == [contract["id"]]


def test_add_requirement_persists_complete_accountability_data(
    api_client: TestClient,
    api_repository: WorkflowRepository,
) -> None:
    workflow = _create_workflow(api_client)

    response = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/requirements",
        json=_requirement_payload(provenance="document_explicit"),
    )

    assert response.status_code == 201
    requirement = response.json()
    reloaded = _get(api_repository, UUID(workflow["id"]))
    assert reloaded is not None
    persisted = reloaded.requirements[0]
    assert str(persisted.id) == requirement["id"]
    assert persisted.owner.email == "owner@example.org"
    assert persisted.supervisor.email == "supervisor@example.org"
    assert persisted.due_date.isoformat() == "2026-12-15"
    assert persisted.target == "50 participants"
    assert persisted.evidence_expectation == "Signed participant records"
    assert persisted.provenance.value == "document_explicit"


def test_ai_inferred_requirement_cannot_start_before_human_approval(
    api_client: TestClient,
    api_repository: WorkflowRepository,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_requirement(api_client, workflow["id"])
    audit_count = len(
        _events(api_repository, UUID(workflow["id"]))
    )

    response = api_client.post(
        _requirement_transition_url(workflow["id"], requirement["id"]),
        json={"action": "start_work"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"
    reloaded = _get(api_repository, UUID(workflow["id"]))
    assert reloaded is not None
    assert reloaded.requirements[0].status == RequirementStatus.NOT_STARTED
    assert reloaded.requirements[0].review_status == ReviewStatus.PENDING_REVIEW
    assert len(_events(api_repository, reloaded.id)) == audit_count


def test_pending_ai_requirement_cannot_be_cancelled_or_audited(
    api_client: TestClient,
    api_repository: WorkflowRepository,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_requirement(api_client, workflow["id"])
    workflow_id = UUID(workflow["id"])
    audit_count = len(_events(api_repository, workflow_id))

    response = api_client.post(
        _requirement_transition_url(workflow["id"], requirement["id"]),
        json={"action": "cancel"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"
    reloaded = _get(api_repository, workflow_id)
    assert reloaded is not None
    persisted = reloaded.requirements[0]
    assert persisted.status == RequirementStatus.NOT_STARTED
    assert persisted.review_status == ReviewStatus.PENDING_REVIEW
    assert len(_events(api_repository, workflow_id)) == audit_count


def test_approved_requirement_can_be_cancelled(
    api_client: TestClient,
    api_repository: WorkflowRepository,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_requirement(api_client, workflow["id"])
    review_url = _requirement_review_url(workflow["id"], requirement["id"])

    approval = api_client.post(
        review_url,
        json={"action": "approve"},
    )
    cancellation = api_client.post(
        _requirement_transition_url(workflow["id"], requirement["id"]),
        json={"action": "cancel"},
    )

    assert approval.status_code == 200
    assert cancellation.status_code == 200
    assert cancellation.json()["status"] == "cancelled"
    assert cancellation.json()["review_status"] == "approved"
    reloaded = _get(api_repository, UUID(workflow["id"]))
    assert reloaded is not None
    assert reloaded.requirements[0].status == RequirementStatus.CANCELLED
    assert reloaded.requirements[0].review_status == ReviewStatus.APPROVED


def test_explicit_approval_enables_valid_requirement_transitions(
    api_client: TestClient,
    api_repository: WorkflowRepository,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_requirement(api_client, workflow["id"])

    approval = api_client.post(
        _requirement_review_url(workflow["id"], requirement["id"]),
        json={
            "action": "approve",
            "note": "Confirmed against the synthetic agreement.",
        },
    )
    started = api_client.post(
        _requirement_transition_url(workflow["id"], requirement["id"]),
        json={"action": "start_work"},
    )
    completed = api_client.post(
        _requirement_transition_url(workflow["id"], requirement["id"]),
        json={"action": "complete"},
    )

    assert approval.status_code == 200
    assert approval.json()["review_status"] == "approved"
    assert approval.json()["is_authoritative"] is True
    assert started.status_code == 200
    assert started.json()["status"] == "in_progress"
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    reloaded = _get(api_repository, UUID(workflow["id"]))
    assert reloaded is not None
    assert reloaded.requirements[0].status == RequirementStatus.COMPLETED
    assert all(
        event.actor_reference == "20000000-0000-0000-0000-000000000001"
        for event in _events(api_repository, reloaded.id)
    )


def test_rejection_and_resubmission_preserve_review_boundary(
    api_client: TestClient,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_requirement(api_client, workflow["id"])
    review_url = _requirement_review_url(workflow["id"], requirement["id"])

    rejected = api_client.post(
        review_url,
        json={
            "action": "reject",
            "reason": "Not supported by the synthetic source.",
        },
    )
    resubmitted = api_client.post(review_url, json={"action": "resubmit"})

    assert rejected.status_code == 200
    assert rejected.json()["review_status"] == "rejected"
    assert rejected.json()["is_authoritative"] is False
    assert resubmitted.status_code == 200
    assert resubmitted.json()["review_status"] == "pending_review"
    assert resubmitted.json()["reviewed_by"] is None


def test_invalid_transition_is_consistent_and_atomic(
    api_client: TestClient,
    api_repository: WorkflowRepository,
) -> None:
    workflow = _create_workflow(api_client)
    workflow_id = UUID(workflow["id"])
    events_before = _events(api_repository, workflow_id)

    response = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/transitions",
        json={"action": "complete"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "invalid_transition",
            "message": "cannot complete a workflow in status draft",
        }
    }
    reloaded = _get(api_repository, workflow_id)
    assert reloaded is not None
    assert reloaded.status == WorkflowStatus.DRAFT
    assert _events(api_repository, workflow_id) == events_before


def test_missing_workflow_and_requirement_return_404(
    api_client: TestClient,
) -> None:
    missing_workflow = api_client.get(f"/api/v1/workflows/{uuid4()}")
    workflow = _create_workflow(api_client)
    missing_requirement = api_client.post(
        _requirement_transition_url(workflow["id"], str(uuid4())),
        json={"action": "start_work"},
    )

    assert missing_workflow.status_code == 404
    assert missing_workflow.json()["error"]["code"] == "not_found"
    assert missing_requirement.status_code == 404
    assert missing_requirement.json()["error"]["code"] == "not_found"


def test_invalid_request_bodies_return_documented_422_format(
    api_client: TestClient,
) -> None:
    missing_name = api_client.post(
        "/api/v1/workflows",
        json={"workflow_type": "grant"},
    )
    invalid_type = api_client.post(
        "/api/v1/workflows",
        json={"name": "Synthetic", "workflow_type": "not_a_type"},
    )

    for response in (missing_name, invalid_type):
        assert response.status_code == 422
        payload = response.json()
        assert payload["error"]["code"] == "request_validation_error"
        assert payload["error"]["message"] == "Request validation failed"
        assert payload["error"]["details"]


def test_browser_requirement_is_saved_and_its_status_reloads(
    api_client: TestClient,
) -> None:
    workflow = _create_workflow(api_client)

    created = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/requirements",
        json=_browser_requirement_payload(),
    )
    assert created.status_code == 201
    requirement = created.json()
    assert requirement["provenance"] == "user_provided"
    assert requirement["review_status"] == "approved"
    assert requirement["is_authoritative"] is True
    assert requirement["status"] == "not_started"
    assert requirement["owner"]["email"] == "owner@example.org"
    assert requirement["due_date"] is None

    reloaded = api_client.get(f"/api/v1/workflows/{workflow['id']}")
    assert reloaded.status_code == 200
    assert reloaded.json()["requirements"][0]["id"] == requirement["id"]

    for action, status in (
        ("start_work", "in_progress"),
        ("block", "blocked"),
        ("start_work", "in_progress"),
        ("complete", "completed"),
    ):
        transition = api_client.post(
            _requirement_transition_url(workflow["id"], requirement["id"]),
            json={"action": action},
        )
        assert transition.status_code == 200
        assert transition.json()["status"] == status
        current = api_client.get(f"/api/v1/workflows/{workflow['id']}")
        assert current.json()["requirements"][0]["status"] == status

    rejected = api_client.post(
        _requirement_transition_url(workflow["id"], requirement["id"]),
        json={"action": "block"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "invalid_transition"
    unchanged = api_client.get(f"/api/v1/workflows/{workflow['id']}")
    assert unchanged.json()["requirements"][0]["status"] == "completed"


def test_requirement_validation_reports_field_errors(
    api_client: TestClient,
) -> None:
    workflow = _create_workflow(api_client)
    payload = _browser_requirement_payload()
    payload["owner"] = {"name": "Synthetic Owner"}

    response = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/requirements",
        json=payload,
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "request_validation_error"
    assert error["details"]
    assert any(item["message"] for item in error["details"])
    stored = api_client.get(f"/api/v1/workflows/{workflow['id']}")
    assert stored.json()["requirements"] == []


def test_ai_inferred_requirement_cannot_be_created_already_approved(
    api_client: TestClient,
) -> None:
    workflow = _create_workflow(api_client)
    forced = _requirement_payload(provenance="ai_inferred")
    forced["review_status"] = "approved"

    rejected = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/requirements",
        json=forced,
    )
    created = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/requirements",
        json=_requirement_payload(provenance="ai_inferred"),
    )

    assert rejected.status_code == 422
    assert created.status_code == 201
    assert created.json()["review_status"] == "pending_review"
    assert created.json()["is_authoritative"] is False
    premature = api_client.post(
        _requirement_transition_url(workflow["id"], created.json()["id"]),
        json={"action": "start_work"},
    )
    assert premature.status_code == 409
    reloaded = api_client.get(f"/api/v1/workflows/{workflow['id']}")
    assert reloaded.json()["requirements"][0]["status"] == "not_started"


def test_browser_requirement_requires_authentication(
    api_client: TestClient,
) -> None:
    saved = dict(api_client.headers)
    api_client.headers.pop("Authorization", None)
    api_client.headers.pop("X-Organization-ID", None)
    try:
        response = api_client.post(
            f"/api/v1/workflows/{uuid4()}/requirements",
            json=_browser_requirement_payload(),
        )
    finally:
        api_client.headers.clear()
        api_client.headers.update(saved)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_browser_requirement_payload_cannot_cross_organizations(
    api_client: TestClient,
    token_factory,
) -> None:
    workflow = _create_workflow(api_client)
    other_headers = {
        "Authorization": f"Bearer {token_factory(user_id=TEST_OTHER_USER_ID)}",
        "X-Organization-ID": str(TEST_OTHER_ORGANIZATION_ID),
    }

    response = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/requirements",
        json=_browser_requirement_payload(),
        headers=other_headers,
    )

    assert response.status_code == 404
    stored = api_client.get(f"/api/v1/workflows/{workflow['id']}")
    assert stored.json()["requirements"] == []


def test_existing_health_endpoint_remains_available(
    api_client: TestClient,
) -> None:
    response = api_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def _create_workflow(
    client: TestClient,
    *,
    name: str = "Synthetic Workflow",
    workflow_type: str = "grant",
) -> dict:
    response = client.post(
        "/api/v1/workflows",
        json={"name": name, "workflow_type": workflow_type},
    )
    assert response.status_code == 201
    return response.json()


def _add_requirement(
    client: TestClient,
    workflow_id: str,
) -> dict:
    response = client.post(
        f"/api/v1/workflows/{workflow_id}/requirements",
        json=_requirement_payload(provenance="ai_inferred"),
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["review_status"] == "pending_review"
    assert payload["is_authoritative"] is False
    return payload


def _browser_requirement_payload() -> dict:
    return {
        "title": "Submit quarterly outcomes report",
        "description": "Submit the synthetic quarterly report.",
        "owner": {
            "name": "Synthetic Owner",
            "email": "owner@example.org",
        },
        "evidence_expectation": "Signed participant records",
        "provenance": "user_provided",
    }


def _requirement_payload(*, provenance: str) -> dict:
    return {
        "title": "Submit quarterly outcomes report",
        "description": "Submit the synthetic quarterly report.",
        "owner": _person_payload(
            name="Synthetic Owner",
            email="owner@example.org",
            role="Program Manager",
        ),
        "supervisor": _person_payload(
            name="Synthetic Supervisor",
            email="supervisor@example.org",
            role="Director",
        ),
        "evidence_expectation": "Signed participant records",
        "provenance": provenance,
        "due_date": "2026-12-15",
        "target": "50 participants",
    }


def _person_payload(
    *,
    name: str,
    email: str,
    role: str | None = None,
) -> dict:
    return {
        "name": name,
        "email": email,
        "role": role,
    }


def _requirement_transition_url(workflow_id: str, requirement_id: str) -> str:
    return (
        f"/api/v1/workflows/{workflow_id}/requirements/"
        f"{requirement_id}/transitions"
    )


def _requirement_review_url(workflow_id: str, requirement_id: str) -> str:
    return (
        f"/api/v1/workflows/{workflow_id}/requirements/"
        f"{requirement_id}/review"
    )


def _get(repository: WorkflowRepository, workflow_id: UUID):
    return repository.get(
        workflow_id,
        organization_id=ORGANIZATION_ID,
    )


def _events(repository: WorkflowRepository, workflow_id: UUID):
    return repository.list_audit_events(
        workflow_id,
        organization_id=ORGANIZATION_ID,
    )
