"""FastAPI application factory and ASGI entrypoint."""

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.health import router as health_router
from app.api.workflows import router as workflows_router
from app.config import get_settings


def create_app() -> FastAPI:
    """Build the API application with system routes only."""

    settings = get_settings()
    expose_docs = settings.environment != "production"

    application = FastAPI(
        title=settings.app_name,
        summary=(
            "What did we promise, who owns it, when is it due, "
            "and what evidence do we have?"
        ),
        version="0.1.0",
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
    )
    register_error_handlers(application)
    application.include_router(health_router)
    application.include_router(workflows_router)
    return application


app = create_app()
