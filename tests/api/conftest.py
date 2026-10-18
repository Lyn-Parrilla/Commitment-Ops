from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api.auth import JWTVerifier, get_jwt_verifier
from app.api.dependencies import (
    get_discovery_repository,
    get_organization_repository,
    get_workflow_repository,
)
from app.config import get_settings
from app.main import create_app
from app.persistence import (
    Base,
    DiscoveryRepository,
    Organization,
    OrganizationAdmin,
    OrganizationRepository,
    SessionFactory,
    WorkflowRepository,
    create_database_engine,
    create_session_factory,
)

TEST_ISSUER = "https://auth.example.test/auth/v1"
TEST_AUDIENCE = "authenticated"
TEST_USER_ID = UUID("20000000-0000-0000-0000-000000000001")
TEST_OTHER_USER_ID = UUID("20000000-0000-0000-0000-000000000002")
TEST_ORGANIZATION_ID = UUID("30000000-0000-0000-0000-000000000001")
TEST_OTHER_ORGANIZATION_ID = UUID(
    "30000000-0000-0000-0000-000000000002"
)


class _SigningKey:
    def __init__(self, key: Any) -> None:
        self.key = key


class _StaticJWKClient:
    def __init__(self, public_key: Any) -> None:
        self._signing_key = _SigningKey(public_key)

    def get_signing_key_from_jwt(self, _: str) -> _SigningKey:
        return self._signing_key


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Iterator[SessionFactory]:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'api-test.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    yield session_factory
    engine.dispose()


@pytest.fixture
def organization_admin(api_session_factory: SessionFactory) -> OrganizationAdmin:
    return OrganizationAdmin(api_session_factory)


@pytest.fixture
def api_repositories(
    api_session_factory: SessionFactory,
    organization_admin: OrganizationAdmin,
) -> tuple[WorkflowRepository, OrganizationRepository]:
    organization_repository = OrganizationRepository(api_session_factory)
    for organization_id, name in (
        (TEST_ORGANIZATION_ID, "Synthetic Organization"),
        (TEST_OTHER_ORGANIZATION_ID, "Other Synthetic Organization"),
    ):
        organization_admin.save(Organization(id=organization_id, name=name))
    organization_admin.set_membership(
        organization_id=TEST_ORGANIZATION_ID,
        user_id=TEST_USER_ID,
        is_active=True,
    )
    organization_admin.set_membership(
        organization_id=TEST_OTHER_ORGANIZATION_ID,
        user_id=TEST_OTHER_USER_ID,
        is_active=True,
    )
    return WorkflowRepository(api_session_factory), organization_repository


@pytest.fixture
def api_repository(
    api_repositories: tuple[WorkflowRepository, OrganizationRepository],
) -> WorkflowRepository:
    return api_repositories[0]


@pytest.fixture(scope="session")
def jwt_private_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def token_factory(jwt_private_key: Any):
    def make_token(
        *,
        user_id: UUID = TEST_USER_ID,
        email: str = "reviewer@example.test",
        expires_delta: timedelta = timedelta(minutes=10),
        signing_key: Any = jwt_private_key,
        issuer: str = TEST_ISSUER,
        audience: str = TEST_AUDIENCE,
        headers: dict[str, Any] | None = None,
        claims: dict[str, Any] | None = None,
        omit: tuple[str, ...] = (),
    ) -> str:
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "sub": str(user_id),
            "email": email,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + expires_delta,
        }
        if claims:
            payload.update(claims)
        for claim in omit:
            payload.pop(claim, None)
        return jwt.encode(
            payload,
            signing_key,
            algorithm="RS256",
            headers=headers,
        )

    return make_token


@pytest.fixture
def api_client(
    api_session_factory: SessionFactory,
    api_repositories: tuple[WorkflowRepository, OrganizationRepository],
    jwt_private_key: Any,
    token_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()
    application = create_app()
    workflow_repository, organization_repository = api_repositories
    verifier = JWTVerifier(
        issuer=TEST_ISSUER,
        audience=TEST_AUDIENCE,
        jwks_url="https://auth.example.test/.well-known/jwks.json",
        jwks_client=_StaticJWKClient(jwt_private_key.public_key()),
    )
    application.dependency_overrides[get_workflow_repository] = (
        lambda: workflow_repository
    )
    application.dependency_overrides[get_organization_repository] = (
        lambda: organization_repository
    )
    application.dependency_overrides[get_discovery_repository] = (
        lambda: DiscoveryRepository(api_session_factory)
    )
    application.dependency_overrides[get_jwt_verifier] = lambda: verifier

    with TestClient(application) as client:
        client.headers.update(
            {
                "Authorization": f"Bearer {token_factory()}",
                "X-Organization-ID": str(TEST_ORGANIZATION_ID),
            }
        )
        yield client

    application.dependency_overrides.clear()
    get_settings.cache_clear()
