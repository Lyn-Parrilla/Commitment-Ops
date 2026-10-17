"""Organization membership and branding for the signed-in user."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.api.auth import AuthenticatedPrincipal, get_authenticated_principal
from app.api.branding import (
    DEFAULT_ACCENT_COLOR,
    DEFAULT_PRIMARY_COLOR,
    resolve_color,
    resolve_display_name,
    resolve_public_url,
)
from app.api.dependencies import get_organization_repository
from app.persistence import Organization, OrganizationRepository

router = APIRouter(prefix="/api/v1", tags=["organizations"])


class OrganizationBrandingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    logo_url: str | None
    primary_color: str
    accent_color: str
    favicon_url: str | None


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    branding: OrganizationBrandingResponse


def branding_response(organization: Organization) -> OrganizationBrandingResponse:
    return OrganizationBrandingResponse(
        display_name=resolve_display_name(organization),
        logo_url=resolve_public_url(organization.logo_url),
        primary_color=resolve_color(
            organization.primary_color,
            DEFAULT_PRIMARY_COLOR,
        ),
        accent_color=resolve_color(
            organization.accent_color,
            DEFAULT_ACCENT_COLOR,
        ),
        favicon_url=resolve_public_url(organization.favicon_url),
    )


@router.get("/organizations", response_model=list[OrganizationResponse])
def list_organizations(
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(get_authenticated_principal),
    ],
    organization_repository: Annotated[
        OrganizationRepository,
        Depends(get_organization_repository),
    ],
) -> list[OrganizationResponse]:
    """Return branding only for organizations this user actively belongs to."""

    return [
        OrganizationResponse(
            id=organization.id,
            name=organization.name,
            branding=branding_response(organization),
        )
        for organization in organization_repository.list_active_for_user(
            principal.user_id
        )
    ]
