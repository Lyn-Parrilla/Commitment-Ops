"""Consistent, bounded API error responses."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.schemas import ErrorDetail, ErrorResponse, validation_issues
from app.application import ResourceNotFoundError
from app.domain import DomainValidationError, InvalidTransitionError
from app.persistence import InvalidAssociationError, PersistenceError


def register_error_handlers(application: FastAPI) -> None:
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
