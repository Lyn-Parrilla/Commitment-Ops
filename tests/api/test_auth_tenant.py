import base64
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from app.api.auth import (
    AuthenticationConfigurationError,
    JWTVerifier,
    _configured_verifier,
    get_jwt_verifier,
)
from app.api.dependencies import (
    get_organization_repository,
    get_workflow_repository,
)
from app.config import get_settings
from app.main import create_app
from app.persistence import OrganizationRepository, WorkflowRepository

USER_ID = UUID("20000000-0000-0000-0000-000000000001")
OTHER_USER_ID = UUID("20000000-0000-0000-0000-000000000002")
ORGANIZATION_ID = UUID("30000000-0000-0000-0000-000000000001")
OTHER_ORGANIZATION_ID = UUID("30000000-0000-0000-0000-000000000002")


def test_missing_authentication_returns_bounded_401(
    api_client: TestClient,
) -> None:
    api_client.headers.pop("Authorization")
    api_client.headers.pop("X-Organization-ID")

    response = api_client.get("/api/v1/workflows")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {
            "code": "authentication_required",
            "message": "Valid bearer authentication is required",
        }
    }


def test_malformed_expired_and_invalidly_signed_tokens_are_rejected(
    api_client: TestClient,
    token_factory,
) -> None:
    invalid_signing_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    tokens = (
        "not-a-jwt",
        token_factory(expires_delta=timedelta(minutes=-1)),
        token_factory(signing_key=invalid_signing_key),
        token_factory(issuer="https://untrusted.example.test/auth/v1"),
        token_factory(audience="untrusted-audience"),
    )

    for token in tokens:
        response = api_client.get(
            "/api/v1/workflows",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_required"
        assert token not in response.text


def test_missing_authentication_configuration_fails_closed_without_details(
    api_repositories: tuple[WorkflowRepository, OrganizationRepository],
    token_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in ("AUTH_ISSUER", "AUTH_AUDIENCE", "AUTH_JWKS_URL"):
        monkeypatch.setenv(variable, "")
    get_settings.cache_clear()
    application = create_app()
    workflows, organizations = api_repositories
    application.dependency_overrides[get_workflow_repository] = lambda: workflows
    application.dependency_overrides[get_organization_repository] = (
        lambda: organizations
    )

    with TestClient(application) as client:
        unauthenticated = client.get("/api/v1/workflows")
        response = client.get(
            "/api/v1/workflows",
            headers={
                "Authorization": f"Bearer {token_factory()}",
                "X-Organization-ID": str(ORGANIZATION_ID),
            },
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "authentication_required"
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "authentication_unavailable",
            "message": "Authentication is temporarily unavailable",
        }
    }
    assert "AUTH_" not in response.text
    assert "DATABASE_URL" not in response.text
    assert "Bearer" not in response.text
    get_settings.cache_clear()


def test_inactive_or_missing_membership_is_denied(
    api_client: TestClient,
    api_repositories: tuple[WorkflowRepository, OrganizationRepository],
    token_factory,
) -> None:
    _, organizations = api_repositories
    organizations.set_membership(
        organization_id=ORGANIZATION_ID,
        user_id=USER_ID,
        is_active=False,
    )

    inactive = api_client.get("/api/v1/workflows")
    missing = api_client.get(
        "/api/v1/workflows",
        headers={
            "Authorization": f"Bearer {token_factory(user_id=OTHER_USER_ID)}",
            "X-Organization-ID": str(ORGANIZATION_ID),
        },
    )

    for response in (inactive, missing):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "organization_access_denied"


def test_lists_and_direct_lookups_do_not_cross_tenant_boundaries(
    api_client: TestClient,
    token_factory,
) -> None:
    workflow = _create_workflow(api_client)
    other_tenant_headers = {
        "Authorization": f"Bearer {token_factory(user_id=OTHER_USER_ID)}",
        "X-Organization-ID": str(OTHER_ORGANIZATION_ID),
    }

    listing = api_client.get(
        "/api/v1/workflows",
        headers=other_tenant_headers,
    )
    lookup = api_client.get(
        f"/api/v1/workflows/{workflow['id']}",
        headers=other_tenant_headers,
    )

    assert listing.status_code == 200
    assert listing.json() == []
    assert lookup.status_code == 404
    assert lookup.json()["error"]["code"] == "not_found"


def test_nested_requirement_mutations_do_not_cross_tenants(
    api_client: TestClient,
    token_factory,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_ai_requirement(api_client, workflow["id"])
    other_tenant_headers = {
        "Authorization": f"Bearer {token_factory(user_id=OTHER_USER_ID)}",
        "X-Organization-ID": str(OTHER_ORGANIZATION_ID),
    }
    transition_url = (
        f"/api/v1/workflows/{workflow['id']}/requirements/"
        f"{requirement['id']}/transitions"
    )
    review_url = (
        f"/api/v1/workflows/{workflow['id']}/requirements/"
        f"{requirement['id']}/review"
    )

    workflow_transition = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/transitions",
        json={"action": "activate"},
        headers=other_tenant_headers,
    )
    addition = api_client.post(
        f"/api/v1/workflows/{workflow['id']}/requirements",
        json=_ai_requirement_payload(),
        headers=other_tenant_headers,
    )
    transition = api_client.post(
        transition_url,
        json={"action": "start_work"},
        headers=other_tenant_headers,
    )
    approval = api_client.post(
        review_url,
        json={"action": "approve"},
        headers=other_tenant_headers,
    )
    rejection = api_client.post(
        review_url,
        json={"action": "reject", "reason": "Synthetic reason"},
        headers=other_tenant_headers,
    )

    assert workflow_transition.status_code == 404
    assert addition.status_code == 404
    assert transition.status_code == 404
    assert approval.status_code == 404
    assert rejection.status_code == 404


def test_review_identity_and_audit_actor_come_from_verified_token(
    api_client: TestClient,
    api_repository: WorkflowRepository,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_ai_requirement(api_client, workflow["id"])
    review_url = (
        f"/api/v1/workflows/{workflow['id']}/requirements/"
        f"{requirement['id']}/review"
    )

    response = api_client.post(
        review_url,
        json={"action": "approve", "note": "Verified human review"},
    )

    assert response.status_code == 200
    reviewer = response.json()["reviewed_by"]
    assert reviewer["id"] == str(USER_ID)
    assert reviewer["email"] == "reviewer@example.test"
    events = api_repository.list_audit_events(
        UUID(workflow["id"]),
        organization_id=ORGANIZATION_ID,
    )
    assert events
    assert all(event.actor_reference == str(USER_ID) for event in events)


def test_client_cannot_supply_authoritative_reviewer_identity(
    api_client: TestClient,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_ai_requirement(api_client, workflow["id"])

    response = api_client.post(
        (
            f"/api/v1/workflows/{workflow['id']}/requirements/"
            f"{requirement['id']}/review"
        ),
        json={
            "action": "approve",
            "reviewer": {
                "id": str(uuid4()),
                "name": "Untrusted Reviewer",
                "email": "untrusted@example.test",
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_client_person_id_cannot_block_another_organizations_review(
    api_client: TestClient,
    api_repository: WorkflowRepository,
    token_factory,
) -> None:
    workflow = _create_workflow(api_client)
    requirement = _add_ai_requirement(api_client, workflow["id"])
    workflow_id = UUID(workflow["id"])
    audit_before = api_repository.list_audit_events(
        workflow_id,
        organization_id=ORGANIZATION_ID,
    )
    other_headers = {
        "Authorization": f"Bearer {token_factory(user_id=OTHER_USER_ID)}",
        "X-Organization-ID": str(OTHER_ORGANIZATION_ID),
    }
    other_workflow = api_client.post(
        "/api/v1/workflows",
        json={"name": "Other organization workflow", "workflow_type": "general"},
        headers=other_headers,
    )
    assert other_workflow.status_code == 201
    other_workflow_id = UUID(other_workflow.json()["id"])
    other_audit_before = api_repository.list_audit_events(
        other_workflow_id,
        organization_id=OTHER_ORGANIZATION_ID,
    )

    rejected = api_client.post(
        f"/api/v1/workflows/{other_workflow.json()['id']}/requirements",
        json=_ai_requirement_payload()
        | {
            "owner": {
                "id": str(USER_ID),
                "name": "Conflicting Owner",
                "email": "other-owner@example.test",
            }
        },
        headers=other_headers,
    )
    approval = api_client.post(
        _requirement_review_url(workflow["id"], requirement["id"]),
        json={"action": "approve", "note": "Verified human review"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "request_validation_error"
    assert "person id is assigned by the server" in rejected.text
    assert str(USER_ID) not in rejected.text
    stored_other = api_repository.get(
        other_workflow_id,
        organization_id=OTHER_ORGANIZATION_ID,
    )
    assert stored_other is not None
    assert stored_other.requirements == []
    assert (
        api_repository.list_audit_events(
            other_workflow_id,
            organization_id=OTHER_ORGANIZATION_ID,
        )
        == other_audit_before
    )
    assert approval.status_code == 200
    assert approval.json()["reviewed_by"]["id"] == str(USER_ID)
    assert approval.json()["review_status"] == "approved"
    audit_after = api_repository.list_audit_events(
        workflow_id,
        organization_id=ORGANIZATION_ID,
    )
    assert len(audit_after) == len(audit_before) + 1
    assert audit_after[-1].actor_reference == str(USER_ID)


def test_disallowed_algorithms_and_key_mismatch_return_bounded_401(
    api_client: TestClient,
    token_factory,
    jwt_private_key,
) -> None:
    signed = token_factory(headers={"kid": "rsa-key"})
    tokens = {
        "none": _replace_algorithm(signed, "none"),
        "HS256": jwt.encode(
            jwt.decode(
                signed,
                jwt_private_key.public_key(),
                algorithms=["RS256"],
                audience="authenticated",
                issuer="https://auth.example.test/auth/v1",
            ),
            "synthetic-hmac-secret-for-tests-only",
            algorithm="HS256",
            headers={"kid": "rsa-key"},
        ),
        "ES256": _replace_algorithm(signed, "ES256"),
    }

    for token in tokens.values():
        response = api_client.get(
            "/api/v1/workflows",
            headers={"Authorization": f"Bearer {token}"},
        )
        _assert_bounded_auth_error(response, 401, token)


def test_invalid_claims_return_bounded_401_without_requiring_email_verified(
    api_client: TestClient,
    token_factory,
) -> None:
    valid = api_client.get(
        "/api/v1/workflows",
        headers={
            "Authorization": (
                f"Bearer {token_factory(claims={'email_verified': False})}"
            )
        },
    )
    now = datetime.now(UTC)
    invalid_tokens = (
        token_factory(claims={"sub": "not-a-uuid"}),
        token_factory(omit=("email",)),
        token_factory(claims={"email": " "}),
        token_factory(claims={"email": "not-an-email"}),
        token_factory(claims={"email": f"a@{'b' * 196}.co"}),
        token_factory(
            claims={
                "iat": now + timedelta(hours=1),
                "exp": now + timedelta(hours=2),
            }
        ),
    )

    assert valid.status_code == 200
    for token in invalid_tokens:
        response = api_client.get(
            "/api/v1/workflows",
            headers={"Authorization": f"Bearer {token}"},
        )
        _assert_bounded_auth_error(response, 401, token)


def test_unknown_signing_key_returns_bounded_401(
    api_repositories: tuple[WorkflowRepository, OrganizationRepository],
    token_factory,
    jwt_private_key,
) -> None:
    jwk = json.loads(RSAAlgorithm.to_jwk(jwt_private_key.public_key()))
    jwk.update({"kid": "known-key", "use": "sig", "alg": "RS256"})
    with _jwks_server({"keys": [jwk]}) as url, _verifier_client(
        api_repositories,
        url,
    ) as client:
        token = token_factory(headers={"kid": "missing-kid-sentinel"})
        response = client.get(
            "/api/v1/workflows",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Organization-ID": str(ORGANIZATION_ID),
            },
        )

    _assert_bounded_auth_error(response, 401, token, "missing-kid-sentinel")


def test_jwks_connection_and_malformed_document_return_bounded_503(
    api_repositories: tuple[WorkflowRepository, OrganizationRepository],
    token_factory,
) -> None:
    token = token_factory(headers={"kid": "known-key"})
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(ORGANIZATION_ID),
    }
    closed_port = _closed_local_port()
    with _verifier_client(
        api_repositories,
        f"http://127.0.0.1:{closed_port}/jwks.json",
    ) as unreachable:
        connection_failure = unreachable.get("/api/v1/workflows", headers=headers)

    with _jwks_server(b"<<<not-json-jwks>>>") as url, _verifier_client(
        api_repositories,
        url,
    ) as malformed_client:
        malformed = malformed_client.get("/api/v1/workflows", headers=headers)

    _assert_bounded_auth_error(
        connection_failure,
        503,
        token,
        str(closed_port),
    )
    _assert_bounded_auth_error(
        malformed,
        503,
        token,
        "<<<not-json-jwks>>>",
    )


def test_production_jwks_url_must_use_https(monkeypatch: pytest.MonkeyPatch) -> None:
    https_url = "https://auth.example.test/auth/v1/.well-known/jwks.json"
    rejected_urls = (
        "http://auth.example.test/auth/v1/.well-known/jwks.json",
        "auth.example.test/jwks.json",
        "https://",
        "://missing-host",
    )

    try:
        for jwks_url in rejected_urls:
            _configure_auth(monkeypatch, jwks_url)
            with pytest.raises(AuthenticationConfigurationError):
                get_jwt_verifier()
        _configure_auth(monkeypatch, https_url)
        assert get_jwt_verifier() is not None
    finally:
        get_settings.cache_clear()
        _configured_verifier.cache_clear()


def test_production_http_jwks_url_returns_bounded_503_without_url_details(
    api_repositories: tuple[WorkflowRepository, OrganizationRepository],
    token_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwks_url = "http://auth.example.test/auth/v1/.well-known/jwks.json"
    _configure_auth(monkeypatch, jwks_url)
    application = create_app()
    workflows, organizations = api_repositories
    application.dependency_overrides[get_workflow_repository] = lambda: workflows
    application.dependency_overrides[get_organization_repository] = (
        lambda: organizations
    )

    try:
        with TestClient(application) as client:
            response = client.get(
                "/api/v1/workflows",
                headers={
                    "Authorization": f"Bearer {token_factory()}",
                    "X-Organization-ID": str(ORGANIZATION_ID),
                },
            )
    finally:
        get_settings.cache_clear()
        _configured_verifier.cache_clear()

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "authentication_unavailable",
            "message": "Authentication is temporarily unavailable",
        }
    }
    assert jwks_url not in response.text
    assert "AUTH_JWKS_URL" not in response.text


def test_health_remains_public(api_client: TestClient) -> None:
    api_client.headers.pop("Authorization")
    api_client.headers.pop("X-Organization-ID")

    response = api_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def _configure_auth(monkeypatch: pytest.MonkeyPatch, jwks_url: str) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_ISSUER", "https://auth.example.test/auth/v1")
    monkeypatch.setenv("AUTH_AUDIENCE", "authenticated")
    monkeypatch.setenv("AUTH_JWKS_URL", jwks_url)
    get_settings.cache_clear()
    _configured_verifier.cache_clear()


def _create_workflow(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/workflows",
        json={"name": "Tenant-isolated workflow", "workflow_type": "grant"},
    )
    assert response.status_code == 201
    assert response.json()["organization_id"] == str(ORGANIZATION_ID)
    return response.json()


def _add_ai_requirement(client: TestClient, workflow_id: str) -> dict:
    response = client.post(
        f"/api/v1/workflows/{workflow_id}/requirements",
        json=_ai_requirement_payload(),
    )
    assert response.status_code == 201
    return response.json()


def _requirement_review_url(workflow_id: str, requirement_id: str) -> str:
    return (
        f"/api/v1/workflows/{workflow_id}/requirements/{requirement_id}/review"
    )


def _replace_algorithm(token: str, algorithm: str) -> str:
    _, payload, signature = token.split(".")
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": algorithm, "typ": "JWT", "kid": "rsa-key"}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def _assert_bounded_auth_error(
    response,
    status_code: int,
    token: str,
    *forbidden: str,
) -> None:
    if status_code == 401:
        envelope = {
            "error": {
                "code": "authentication_required",
                "message": "Valid bearer authentication is required",
            }
        }
    else:
        envelope = {
            "error": {
                "code": "authentication_unavailable",
                "message": "Authentication is temporarily unavailable",
            }
        }
    assert response.status_code == status_code
    assert response.json() == envelope
    assert token not in response.text
    lowered = response.text.lower()
    assert "traceback" not in lowered
    assert "pem" not in lowered
    for value in forbidden:
        assert value not in response.text


def _closed_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _jwks_server(payload: dict[str, Any] | bytes) -> Iterator[str]:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/.well-known/jwks.json"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def _verifier_client(
    repositories: tuple[WorkflowRepository, OrganizationRepository],
    jwks_url: str,
) -> Iterator[TestClient]:
    application = create_app()
    workflows, organizations = repositories
    application.dependency_overrides[get_workflow_repository] = lambda: workflows
    application.dependency_overrides[get_organization_repository] = (
        lambda: organizations
    )
    application.dependency_overrides[get_jwt_verifier] = lambda: JWTVerifier(
        issuer="https://auth.example.test/auth/v1",
        audience="authenticated",
        jwks_url=jwks_url,
    )
    with TestClient(application) as client:
        yield client
    application.dependency_overrides.clear()


def _ai_requirement_payload() -> dict:
    return {
        "title": "Review synthetic report",
        "description": "A synthetic inferred obligation.",
        "owner": {
            "name": "Synthetic Owner",
            "email": "owner@example.test",
        },
        "evidence_expectation": "Synthetic report",
        "provenance": "ai_inferred",
    }
