"""HTTP review decisions use the authenticated reviewer and stay tenant scoped."""

from uuid import uuid4

from fastapi.testclient import TestClient

from tests.api.conftest import (
    TEST_ORGANIZATION_ID,
    TEST_OTHER_ORGANIZATION_ID,
    TEST_OTHER_USER_ID,
    TEST_USER_ID,
)

_CANDIDATE = {
    "title": "Quarterly narrative",
    "description": "File the narrative and budget-to-actuals.",
    "owner": {"name": "Alex Rivera", "email": "alex.rivera@example.org"},
    "evidence_expectation": "Signed narrative",
    "provenance": "ai_inferred",
    "source_filename": "award.pdf",
    "source_document_type": "grant_agreement",
    "source_reference": "grants/award.pdf",
    "source_location": {"page": 4, "section": "Reporting"},
    "discovery_reason": "The section uses mandatory reporting language.",
    "confidence": 0.5,
}


def test_approval_persists_decision_signal_and_provenance(
    api_client: TestClient,
) -> None:
    created = _discover(api_client)
    assert created["is_authoritative"] is False
    assert created["review_status"] == "pending_review"
    assert created["learning_signals"] == []

    reviewed = api_client.post(
        f"/api/v1/discovery/candidates/{created['id']}/review",
        json={"action": "approve", "note": "Confirmed the clause."},
    )

    assert reviewed.status_code == 200
    body = reviewed.json()
    assert body["is_authoritative"] is False
    assert body["review_status"] == "approved"
    assert body["reviewed_by"]["id"] == str(TEST_USER_ID)
    assert body["reviewed_at"] is not None
    signal = body["learning_signals"][0]
    assert signal["decision_id"] != signal["id"]
    assert signal["human_decision"] == "approved"
    assert signal["reviewer"]["id"] == str(TEST_USER_ID)
    assert signal["decided_at"] == body["reviewed_at"]
    assert signal["source_filename"] == "award.pdf"
    assert signal["source_reference"] == "grants/award.pdf"
    assert signal["source_location"]["page"] == 4
    assert signal["source_location"]["section"] == "Reporting"
    assert signal["original_title"] == "Quarterly narrative"
    assert signal["resulting_requirement_id"] is not None
    assert signal["resulting_provenance"] == "ai_inferred"

    reloaded = api_client.get(f"/api/v1/discovery/candidates/{created['id']}")
    assert reloaded.status_code == 200
    assert reloaded.json()["learning_signals"] == body["learning_signals"]
    assert reloaded.json()["source_document_id"] == body["source_document_id"]


def test_rejection_edit_and_repeat_review_are_deterministic(
    api_client: TestClient,
) -> None:
    created = _discover(api_client)
    rejected = api_client.post(
        f"/api/v1/discovery/candidates/{created['id']}/review",
        json={"action": "reject", "reason": "Owner classification is incomplete."},
    )
    assert rejected.status_code == 200
    assert rejected.json()["learning_signals"][0]["resulting_requirement_id"] is None

    revised = api_client.post(
        f"/api/v1/discovery/candidates/{created['id']}/revisions",
        json={"title": "Quarterly financial narrative"},
    )
    assert revised.status_code == 200
    revised_body = revised.json()
    assert revised_body["review_status"] == "pending_review"
    assert revised_body["reviewed_by"] is None
    assert revised_body["title"] == "Quarterly financial narrative"
    assert revised_body["is_authoritative"] is False
    assert len(revised_body["learning_signals"]) == 1
    assert revised_body["learning_signals"][0]["original_title"] == "Quarterly narrative"

    repeated = api_client.post(
        f"/api/v1/discovery/candidates/{created['id']}/review",
        json={"action": "reject", "reason": "Still incomplete."},
    )
    assert repeated.status_code == 200
    assert len(repeated.json()["learning_signals"]) == 2

    approved = api_client.post(
        f"/api/v1/discovery/candidates/{created['id']}/review",
        json={"action": "approve", "note": "Now complete."},
    )
    assert approved.status_code == 409
    duplicate = api_client.post(
        f"/api/v1/discovery/candidates/{created['id']}/review",
        json={"action": "reject", "reason": "Again."},
    )
    assert duplicate.status_code == 409
    stored = api_client.get(f"/api/v1/discovery/candidates/{created['id']}")
    assert len(stored.json()["learning_signals"]) == 2


def test_invalid_review_actions_do_not_record_feedback(
    api_client: TestClient,
) -> None:
    incomplete = api_client.post(
        "/api/v1/discovery/candidates",
        json={
            "title": "Potential report",
            "provenance": "ai_inferred",
            "source_filename": "notes.pdf",
            "source_document_type": "other",
            "source_reference": "notes/potential.pdf",
        },
    )
    assert incomplete.status_code == 201
    candidate_id = incomplete.json()["id"]
    approval = api_client.post(
        f"/api/v1/discovery/candidates/{candidate_id}/review",
        json={"action": "approve"},
    )
    assert approval.status_code == 422
    assert api_client.get(f"/api/v1/discovery/candidates/{candidate_id}").json()[
        "learning_signals"
    ] == []

    created = _discover(api_client)
    missing_reason = api_client.post(
        f"/api/v1/discovery/candidates/{created['id']}/review",
        json={"action": "reject"},
    )
    assert missing_reason.status_code == 422
    client_identity = api_client.post(
        f"/api/v1/discovery/candidates/{created['id']}/review",
        json={
            "action": "approve",
            "note": "Confirmed.",
            "reviewer_id": str(uuid4()),
            "decided_at": "2020-01-01T00:00:00Z",
        },
    )
    assert client_identity.status_code == 422
    approved = api_client.post(
        f"/api/v1/discovery/candidates/{created['id']}/review",
        json={"action": "approve", "note": "Confirmed."},
    )
    assert approved.status_code == 200
    assert approved.json()["reviewed_by"]["id"] == str(TEST_USER_ID)
    edited = api_client.post(
        f"/api/v1/discovery/candidates/{created['id']}/revisions",
        json={"title": "Changed after approval"},
    )
    assert edited.status_code == 409
    assert (
        api_client.get(f"/api/v1/discovery/candidates/{created['id']}").json()["title"]
        == "Quarterly narrative"
    )


def test_human_added_feedback_is_separate_and_reloads(
    api_client: TestClient,
) -> None:
    created = api_client.post(
        "/api/v1/discovery/human-added",
        json={
            "title": "Missed insurance certificate",
            "description": "File the certificate before work starts.",
            "owner": {"name": "Alex Rivera", "email": "alex.rivera@example.org"},
            "evidence_expectation": "Certificate on file",
            "provenance": "user_provided",
            "source_filename": "vendor.pdf",
            "source_document_type": "contract",
            "source_reference": "vendors/vendor.pdf",
            "source_location": {"page": 2, "heading": "Insurance"},
            "reason": "The extractor skipped the certificate clause.",
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["candidate_id"] is None
    assert body["decision_id"] != body["id"]
    assert body["human_decision"] == "human_added"
    assert body["reviewer"]["id"] == str(TEST_USER_ID)
    assert body["source_location"]["heading"] == "Insurance"
    assert body["resulting_provenance"] == "user_provided"
    reloaded = api_client.get(f"/api/v1/discovery/signals/{body['id']}")
    assert reloaded.status_code == 200
    assert reloaded.json() == body

    inferred = api_client.post(
        "/api/v1/discovery/human-added",
        json={
            "title": "Inferred without review",
            "description": "This must not become authoritative feedback.",
            "owner": {"name": "Alex Rivera", "email": "alex.rivera@example.org"},
            "evidence_expectation": "None",
            "provenance": "ai_inferred",
            "source_filename": "draft.pdf",
            "source_document_type": "other",
            "source_reference": "drafts/draft.pdf",
        },
    )
    assert inferred.status_code == 422


def test_review_records_are_not_visible_to_another_organization(
    api_client: TestClient,
    token_factory,
) -> None:
    created = _discover(api_client)
    response = api_client.get(
        f"/api/v1/discovery/candidates/{created['id']}",
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

    assert response.status_code == 404
    assert "award.pdf" not in response.text
    assert "Quarterly narrative" not in response.text


def _discover(client: TestClient) -> dict:
    response = client.post("/api/v1/discovery/candidates", json=_CANDIDATE)
    assert response.status_code == 201
    return response.json()
