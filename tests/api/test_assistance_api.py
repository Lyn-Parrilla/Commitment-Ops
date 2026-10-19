"""Authenticated assistance proposals stay bound to the caller's organization."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

USER_ID = UUID("20000000-0000-0000-0000-000000000001")
OTHER_USER_ID = UUID("20000000-0000-0000-0000-000000000002")
OTHER_ORGANIZATION_ID = UUID("30000000-0000-0000-0000-000000000002")


def test_blocked_proposal_requires_approval_before_a_single_execution(
    api_client: TestClient,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_blocked_requirement(api_client, workflow["id"])
    before = datetime.now(UTC)

    detected = api_client.post(_collection(workflow["id"]))
    assert detected.status_code == 200
    proposal = detected.json()[0]
    assert proposal["recommendation_type"] == "blocked_requirement"
    assert proposal["state"] == "proposed"
    assert proposal["events"][0]["actor_user_id"] == str(USER_ID)
    assert proposal["events"][0]["event_type"] == "proposed"
    occurred_at = datetime.fromisoformat(
        proposal["events"][0]["occurred_at"].replace("Z", "+00:00")
    )
    assert before - timedelta(seconds=1) <= occurred_at <= datetime.now(UTC)
    assert "owner@example.org" not in proposal["rationale"]

    premature = api_client.post(_execute(workflow["id"], proposal["id"]))
    assert premature.status_code == 409

    untrusted = api_client.post(
        _decision(workflow["id"], proposal["id"]),
        json={
            "action": "approve",
            "reviewer_id": str(uuid4()),
            "decided_at": "2020-01-01T00:00:00Z",
        },
    )
    assert untrusted.status_code == 422

    approved = api_client.post(
        _decision(workflow["id"], proposal["id"]),
        json={"action": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert approved.json()["events"][-1]["actor_user_id"] == str(USER_ID)

    executed = api_client.post(_execute(workflow["id"], proposal["id"]))
    assert executed.status_code == 200
    body = executed.json()
    assert body["state"] == "executed"
    assert [event["event_type"] for event in body["events"]] == [
        "proposed",
        "approved",
        "executed",
        "outcome_recorded",
    ]
    assert body["events"][-1]["detail"]["result"] == "not_achieved"

    repeated = api_client.post(_execute(workflow["id"], proposal["id"]))
    assert repeated.status_code == 409
    reloaded = api_client.get(_proposal(workflow["id"], proposal["id"]))
    assert reloaded.status_code == 200
    assert reloaded.json()["events"][-1]["detail"]["result"] == "not_achieved"
    assert str(requirement["id"]) == proposal["requirement_id"]


def test_rejected_proposal_cannot_be_executed(api_client: TestClient) -> None:
    workflow = _create_workflow(api_client)
    _add_blocked_requirement(api_client, workflow["id"])
    proposal = api_client.post(_collection(workflow["id"])).json()[0]

    rejected = api_client.post(
        _decision(workflow["id"], proposal["id"]),
        json={"action": "reject"},
    )

    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"
    assert (
        api_client.post(_execute(workflow["id"], proposal["id"])).status_code
        == 409
    )


def test_overdue_requirement_is_measured_when_it_is_no_longer_past_due(
    api_client: TestClient,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_requirement(api_client, workflow["id"], due_date="2000-01-01")
    proposal = api_client.post(_collection(workflow["id"])).json()[0]
    assert proposal["recommendation_type"] == "overdue_requirement"
    api_client.post(
        _decision(workflow["id"], proposal["id"]),
        json={"action": "approve"},
    )
    completed = api_client.post(
        (
            f"/api/v1/workflows/{workflow['id']}/requirements/"
            f"{requirement['id']}/transitions"
        ),
        json={"action": "complete"},
    )
    assert completed.status_code == 200

    executed = api_client.post(_execute(workflow["id"], proposal["id"]))

    assert executed.status_code == 200
    assert executed.json()["events"][-1]["detail"]["result"] == "achieved"


def test_decision_and_execution_require_authentication_and_membership(
    api_client: TestClient,
    organization_admin,
    token_factory,
) -> None:
    workflow = _create_workflow(api_client)
    _add_blocked_requirement(api_client, workflow["id"])
    proposal = api_client.post(_collection(workflow["id"])).json()[0]
    api_client.headers.pop("Authorization")

    anonymous_decision = api_client.post(
        _decision(workflow["id"], proposal["id"]),
        json={"action": "approve"},
    )
    anonymous_execution = api_client.post(_execute(workflow["id"], proposal["id"]))

    assert anonymous_decision.status_code == 401
    assert anonymous_execution.status_code == 401
    organization_admin.set_membership(
        organization_id=UUID("30000000-0000-0000-0000-000000000001"),
        user_id=USER_ID,
        is_active=False,
    )
    api_client.headers["Authorization"] = f"Bearer {token_factory()}"
    inactive_decision = api_client.post(
        _decision(workflow["id"], proposal["id"]),
        json={"action": "approve"},
    )
    inactive_execution = api_client.post(_execute(workflow["id"], proposal["id"]))

    assert inactive_decision.status_code == 403
    assert inactive_execution.status_code == 403


def test_proposal_cannot_be_executed_through_another_workflow(
    api_client: TestClient,
) -> None:
    workflow = _create_workflow(api_client)
    other = _create_workflow(api_client)
    _add_blocked_requirement(api_client, workflow["id"])
    proposal = api_client.post(_collection(workflow["id"])).json()[0]
    api_client.post(
        _decision(workflow["id"], proposal["id"]),
        json={"action": "approve"},
    )

    decision = api_client.post(
        _decision(other["id"], proposal["id"]),
        json={"action": "reject"},
    )
    executed = api_client.post(_execute(other["id"], proposal["id"]))
    queried = api_client.post(
        _execute(workflow["id"], proposal["id"]),
        params={"organization_id": str(OTHER_ORGANIZATION_ID)},
    )

    assert decision.status_code == 404
    assert executed.status_code == 404
    assert queried.status_code == 200
    assert queried.json()["state"] == "executed"


def test_other_organization_cannot_execute_a_proposal(
    api_client: TestClient,
    token_factory,
) -> None:
    workflow = _create_workflow(api_client)
    _add_blocked_requirement(api_client, workflow["id"])
    proposal = api_client.post(_collection(workflow["id"])).json()[0]
    other_headers = {
        "Authorization": f"Bearer {token_factory(user_id=OTHER_USER_ID)}",
        "X-Organization-ID": str(OTHER_ORGANIZATION_ID),
    }

    read = api_client.get(
        _proposal(workflow["id"], proposal["id"]),
        headers=other_headers,
    )
    executed = api_client.post(
        _execute(workflow["id"], proposal["id"]),
        headers=other_headers,
    )

    assert read.status_code == 404
    assert executed.status_code == 404
    assert proposal["rationale"] not in read.text
    assert proposal["rationale"] not in executed.text


def _create_workflow(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/workflows",
        json={"name": "Synthetic workflow", "workflow_type": "general"},
    )
    assert response.status_code == 201
    return response.json()


def _add_requirement(
    client: TestClient,
    workflow_id: str,
    *,
    due_date: str,
) -> dict:
    response = client.post(
        f"/api/v1/workflows/{workflow_id}/requirements",
        json={
            "title": "Synthetic requirement",
            "description": "Record the factual outcome.",
            "owner": {
                "name": "Synthetic Owner",
                "email": "owner@example.org",
            },
            "evidence_expectation": "A synthetic record",
            "provenance": "user_provided",
            "due_date": due_date,
        },
    )
    assert response.status_code == 201
    return response.json()


def _add_blocked_requirement(client: TestClient, workflow_id: str) -> dict:
    requirement = _add_requirement(
        client,
        workflow_id,
        due_date="2099-01-01",
    )
    for action in ("start_work", "block"):
        response = client.post(
            (
                f"/api/v1/workflows/{workflow_id}/requirements/"
                f"{requirement['id']}/transitions"
            ),
            json={"action": action},
        )
        assert response.status_code == 200
    return requirement


def _collection(workflow_id: str) -> str:
    return f"/api/v1/workflows/{workflow_id}/assistance-proposals"


def _proposal(workflow_id: str, proposal_id: str) -> str:
    return f"{_collection(workflow_id)}/{proposal_id}"


def _decision(workflow_id: str, proposal_id: str) -> str:
    return f"{_proposal(workflow_id, proposal_id)}/decision"


def _execute(workflow_id: str, proposal_id: str) -> str:
    return f"{_proposal(workflow_id, proposal_id)}/execute"
