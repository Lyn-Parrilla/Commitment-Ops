"""Consistent, bounded API error responses."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.auth import (
    AuthenticationConfigurationError,
    AuthenticationError,
    AuthenticationServiceError,
    OrganizationAccessDeniedError,
)
from app.api.schemas import ErrorDetail, ErrorResponse, validation_issues
from app.application import ResourceNotFoundError
from app.application.exceptions import EvidenceFileUnavailableError
from app.domain import DomainValidationError, InvalidTransitionError
from app.persistence import InvalidAssociationError, PersistenceError


def register_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(AuthenticationError)
    async def authentication_handler(
        _: Request,
        __: AuthenticationError,
    ) -> JSONResponse:
        response = _response(
            401,
            code="authentication_required",
            message="Valid bearer authentication is required",
        )
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @application.exception_handler(AuthenticationConfigurationError)
    @application.exception_handler(AuthenticationServiceError)
    async def authentication_unavailable_handler(
        _: Request,
        __: AuthenticationConfigurationError | AuthenticationServiceError,
    ) -> JSONResponse:
        return _response(
            503,
            code="authentication_unavailable",
            message="Authentication is temporarily unavailable",
        )

    @application.exception_handler(OrganizationAccessDeniedError)
    async def organization_access_denied_handler(
        _: Request,
        __: OrganizationAccessDeniedError,
    ) -> JSONResponse:
        return _response(
            403,
            code="organization_access_denied",
            message="Active organization membership is required",
        )

    @application.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return _response(
            422,
            code="request_validation_error",
            message="Request validation failed",
            details=validation_issues(exc.errors()),
        )

    @application.exception_handler(EvidenceFileUnavailableError)
    async def evidence_file_unavailable_handler(
        _: Request,
        exc: EvidenceFileUnavailableError,
    ) -> JSONResponse:
        return _response(
            409,
            code="evidence_file_unavailable",
            message=str(exc),
        )

    @application.exception_handler(ResourceNotFoundError)
    async def not_found_handler(
        _: Request,
        exc: ResourceNotFoundError,
    ) -> JSONResponse:
        return _response(404, code="not_found", message=str(exc))

    @application.exception_handler(InvalidTransitionError)
    async def invalid_transition_handler(
        _: Request,
        exc: InvalidTransitionError,
    ) -> JSONResponse:
        return _response(
            409,
            code="invalid_transition",
            message=str(exc),
        )

    @application.exception_handler(DomainValidationError)
    async def domain_validation_handler(
        _: Request,
        exc: DomainValidationError,
    ) -> JSONResponse:
        return _response(
            422,
            code="domain_validation_error",
            message=str(exc),
        )

    @application.exception_handler(InvalidAssociationError)
    async def invalid_association_handler(
        _: Request,
        exc: InvalidAssociationError,
    ) -> JSONResponse:
        return _response(
            409,
            code="invalid_association",
            message=str(exc),
        )

    @application.exception_handler(PersistenceError)
    async def persistence_handler(
        _: Request,
        __: PersistenceError,
    ) -> JSONResponse:
        return _response(
            503,
            code="persistence_unavailable",
            message="The accountability store could not complete the operation",
        )


def _response(
    status_code: int,
    *,
    code: str,
    message: str,
    details: list | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json", exclude_none=True),
    )
