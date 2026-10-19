"""FastAPI application factory and ASGI entrypoint."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.assistance import router as assistance_router
from app.api.client_config import router as client_config_router
from app.api.discovery import router as discovery_router
from app.api.errors import register_error_handlers
from app.api.health import router as health_router
from app.api.organizations import router as organizations_router
from app.api.workflows import router as workflows_router
from app.config import get_settings

WEB_ROOT = Path(__file__).resolve().parent / "web"


def create_app() -> FastAPI:
    """Build the API and the same-origin browser interface."""

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
    application.include_router(client_config_router)
    application.include_router(organizations_router)
    application.include_router(workflows_router)
    application.include_router(discovery_router)
    application.include_router(assistance_router)
    _install_web(application)
    return application


def _install_web(application: FastAPI) -> None:
    application.mount(
        "/assets",
        StaticFiles(directory=WEB_ROOT / "assets"),
        name="web-assets",
    )

    @application.get("/", include_in_schema=False)
    def web_index() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")


app = create_app()
