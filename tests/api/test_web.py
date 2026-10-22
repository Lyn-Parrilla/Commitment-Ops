import json
import subprocess
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.persistence import Organization, OrganizationAdmin

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


def test_detail_page_adds_user_provided_requirements_and_status_actions() -> None:
    script = (
        Path(__file__).parents[2] / "app" / "web" / "assets" / "app.js"
    ).read_text()

    assert 'provenance: "user_provided"' in script
    assert 'provenance: "ai_inferred"' not in script
    assert "review_status:" not in script
    assert "requirement.review_status" in script
    assert "/requirements/${requirement.id}/transitions" in script
    assert "start_work" in script
    assert '["block", "Block"]' in script
    assert '["complete", "Complete"]' in script
    assert "body: { action }" in script
    assert "X-Organization-ID" in script
    assert "organization_id" not in script
    assert "requirement.status =" not in script.replace("requirement.status ===", "")
    assert "reloadWorkflow" in script
    assert "submit.disabled = true" in script
    assert "button.disabled = true" in script
    assert "payload?.error?.details" in script


def test_attention_rules_group_requirements_by_local_calendar_date() -> None:
    source = (
        Path(__file__).parents[2] / "app" / "web" / "assets" / "app.js"
    ).read_text()
    start = source.index("/* attention-rules */")
    end = source.index("/* end-attention-rules */")
    script = """
const WORKFLOW_TYPES = [
  ["grant", "Grant"],
  ["compliance", "Compliance"],
  ["vendor_contract", "Vendor contract"],
  ["customer_implementation", "Customer implementation"],
  ["regulatory_reporting", "Regulatory reporting"],
  ["general", "General"],
];
""" + source[start:end] + """
const today = { year: 2026, month: 10, day: 20 };
const requirement = (fields) => ({
  title: "Quarterly report",
  status: "not_started",
  review_status: "approved",
  provenance: "user_provided",
  owner: { name: "Ada Lovelace" },
  due_date: "2026-12-01",
  evidence: [],
  ...fields,
});
const workflows = [
  {
    id: "workflow-1",
    name: "Veteran housing grant",
    requirements: [
      requirement({ due_date: "2026-10-20" }),
      requirement({ due_date: "2026-10-19", status: "in_progress" }),
      requirement({ due_date: "2026-10-21" }),
      requirement({ due_date: "2026-10-01", status: "completed" }),
      requirement({ due_date: "2026-10-01", status: "cancelled" }),
      requirement({
        title: "Blocked intake",
        status: "blocked",
        review_status: "pending_review",
        due_date: "2026-10-18",
        provenance: "ai_inferred",
      }),
      requirement({
        title: "Awaiting only",
        review_status: "pending_review",
        due_date: null,
        owner: null,
      }),
      requirement({
        title: "Blocked only",
        status: "blocked",
        due_date: "",
        owner: {},
      }),
    ],
  },
  { name: "Missing id", requirements: [requirement({ status: "blocked" })] },
];
const items = attentionItems(workflows, today);
const utcMidnight = new Date(Date.UTC(2026, 9, 20, 0, 30));
process.stdout.write(JSON.stringify({
  items,
  localFromClock: localCalendarDate(utcMidnight),
  localGetters: {
    year: utcMidnight.getFullYear(),
    month: utcMidnight.getMonth() + 1,
    day: utcMidnight.getDate(),
  },
  parsed: parseCalendarDate("2026-10-20"),
  dueToday: isPastDue(requirement({ due_date: "2026-10-20" }), today),
  dueYesterday: isPastDue(requirement({ due_date: "2026-10-19" }), today),
  labels: {
    work: readableLabel("in_progress", WORK_LABELS),
    review: readableLabel("pending_review", REVIEW_LABELS),
    type: readableLabel("vendor_contract", WORKFLOW_TYPE_LABELS),
    provenance: provenanceLabel(requirement({
      provenance: "ai_inferred",
      review_status: "pending_review",
    })),
    entered: provenanceLabel(requirement({ provenance: "user_provided" })),
    absent: provenanceLabel(requirement({ provenance: "" })),
  },
  formatted: formatCalendarDate("2026-10-19"),
}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    titles = [item["title"] for item in payload["items"]]

    assert titles == [
        "Quarterly report",
        "Blocked intake",
        "Awaiting only",
        "Blocked only",
    ]
    assert payload["items"][0]["reasons"] == ["Past due"]
    assert payload["items"][0]["ownerName"] == "Ada Lovelace"
    assert payload["items"][0]["dueDate"] == "2026-10-19"
    assert payload["items"][1]["reasons"] == [
        "Past due",
        "Blocked",
        "Awaiting review",
    ]
    assert payload["items"][1]["workflowName"] == "Veteran housing grant"
    assert payload["items"][2]["reasons"] == ["Awaiting review"]
    assert payload["items"][2]["ownerName"] == ""
    assert payload["items"][2]["dueDate"] == ""
    assert payload["items"][3]["reasons"] == ["Blocked"]
    assert payload["items"][3]["ownerName"] == ""
    assert payload["dueToday"] is False
    assert payload["dueYesterday"] is True
    assert payload["parsed"] == {"year": 2026, "month": 10, "day": 20}
    assert payload["localFromClock"] == payload["localGetters"]
    assert payload["labels"] == {
        "work": "In progress",
        "review": "Awaiting review",
        "type": "Vendor contract",
        "provenance": "Suggested, awaiting approval",
        "entered": "Entered by a person",
        "absent": "",
    }
    assert "2026" in payload["formatted"]
    assert "19" in payload["formatted"]
    assert "18" not in payload["formatted"]


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
    organization_admin: OrganizationAdmin,
) -> None:
    organization_admin.save(
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
    organization_admin: OrganizationAdmin,
) -> None:
    organization_admin.set_membership(
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
