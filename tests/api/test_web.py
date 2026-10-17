from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.persistence import Organization, OrganizationRepository, WorkflowRepository

TEST_USER_ID = UUID("20000000-0000-0000-0000-000000000001")
TEST_ORGANIZATION_ID = UUID("30000000-0000-0000-0000-000000000001")
TEST_OTHER_ORGANIZATION_ID = UUID("30000000-0000-0000-0000-000000000002")


def test_browser_assets_are_served_with_the_api(client: TestClient) -> None:
    page = client.get("/")
    script = client.get("/assets/app.js")
    style = client.get("/assets/styles.css")
    icon = client.get("/assets/favicon.svg")

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "/assets/app.js" in page.text
    assert script.status_code == 200
    assert "X-Organization-ID" in script.text
    assert "SERVICE_ROLE" not in script.text
    assert "DATABASE_URL" not in script.text
    assert style.status_code == 200
    assert icon.status_code == 200
    assert client.get("/health").status_code == 200


def test_image_packages_the_browser_assets() -> None:
    repository_root = Path(__file__).parents[2]
    dockerfile = (repository_root / "Dockerfile").read_text()
    dockerignore = (repository_root / ".dockerignore").read_text()

    assert "COPY app ./app" in dockerfile
    assert "app/web" not in dockerignore
    assert (repository_root / "app" / "web" / "index.html").is_file()
    assert (repository_root / "app" / "web" / "assets" / "app.js").is_file()


def test_client_config_exposes_only_the_publishable_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "publishable-anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-must-stay-server-side")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:database-secret@db.example/postgres",
    )
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/client-config")

    get_settings.cache_clear()
    assert response.status_code == 200
    assert response.json() == {
        "supabase_url": "https://project.supabase.co",
        "supabase_anon_key": "publishable-anon-key",
    }
    assert "service-role-must-stay-server-side" not in response.text
    assert "database-secret" not in response.text


def test_invalid_client_config_is_omitted(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "http://project.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "postgresql://user:secret@db/postgres")
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/client-config")

    get_settings.cache_clear()
    assert response.status_code == 200
    assert response.json() == {"supabase_url": None, "supabase_anon_key": None}


def test_organization_branding_is_membership_scoped_and_safe(
    api_client: TestClient,
    api_repositories: tuple[WorkflowRepository, OrganizationRepository],
) -> None:
    _, organizations = api_repositories
    organizations.save(
        Organization(
            id=TEST_ORGANIZATION_ID,
            name="Synthetic Organization",
            display_name="Northwind Grants",
            logo_url="https://cdn.example.test/logo.png",
            primary_color="#112233",
            accent_color="red",
            favicon_url="https://user:secret@cdn.example.test/favicon.png",
        )
    )

    response = api_client.get(
        "/api/v1/organizations",
        headers={"X-Organization-ID": str(TEST_OTHER_ORGANIZATION_ID)},
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(TEST_ORGANIZATION_ID),
            "name": "Synthetic Organization",
            "branding": {
                "display_name": "Northwind Grants",
                "logo_url": "https://cdn.example.test/logo.png",
                "primary_color": "#112233",
                "accent_color": "#B08D57",
                "favicon_url": None,
            },
        }
    ]


def test_missing_branding_uses_safe_defaults(
    api_client: TestClient,
) -> None:
    response = api_client.get("/api/v1/organizations")

    assert response.status_code == 200
    branding = response.json()[0]["branding"]
    assert branding["display_name"] == "Synthetic Organization"
    assert branding["logo_url"] is None
    assert branding["primary_color"] == "#1B3A4B"
    assert branding["accent_color"] == "#B08D57"
    assert branding["favicon_url"] is None


def test_inactive_membership_is_not_returned(
    api_client: TestClient,
    api_repositories: tuple[WorkflowRepository, OrganizationRepository],
) -> None:
    _, organizations = api_repositories
    organizations.set_membership(
        organization_id=TEST_ORGANIZATION_ID,
        user_id=TEST_USER_ID,
        is_active=False,
    )

    response = api_client.get("/api/v1/organizations")

    assert response.status_code == 200
    assert response.json() == []


def test_organization_list_requires_authentication(api_client: TestClient) -> None:
    api_client.headers.pop("Authorization")

    response = api_client.get("/api/v1/organizations")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
