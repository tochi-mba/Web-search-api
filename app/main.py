"""Application factory and global exception handling."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.bootstrap import build_services
from app.config import Settings, get_settings
from app.core.errors import DomainError, ValidationProblem
from app.core.logging import configure_logging, get_logger
from app.core.middleware import build_auth_middleware, request_context_middleware
from app.services.fetch.browser import resolve_executable_path

logger = get_logger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"


def _problem_response(problem: dict[str, object], status: int) -> JSONResponse:
    """Render a problem document with the correct media type."""
    return JSONResponse(status_code=status, content=problem, media_type=PROBLEM_MEDIA_TYPE)


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a :class:`DomainError` as RFC 9457 problem+json."""
    assert isinstance(exc, DomainError)  # noqa: S101 - handler is registered per type
    exc.instance = exc.instance or request.url.path
    logger.warning("request.domain_error", code=exc.code, status=exc.status, title=exc.title)
    return _problem_response(exc.to_problem(), exc.status)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render FastAPI request-validation failures as problem+json."""
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    problem = ValidationProblem(
        "Request validation failed",
        detail="One or more fields were invalid.",
        instance=request.url.path,
    ).to_problem()
    problem["errors"] = [
        {"loc": [str(part) for part in err.get("loc", ())], "msg": err.get("msg", "")}
        for err in exc.errors()
    ]
    return _problem_response(problem, 422)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render Starlette HTTP errors (404s and friends) as problem+json."""
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    problem = {
        "type": "https://web-search-api.dev/problems/http_error",
        "title": str(exc.detail),
        "status": exc.status_code,
        "detail": str(exc.detail),
        "code": "http_error",
        "instance": request.url.path,
    }
    return _problem_response(problem, exc.status_code)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: never leak a traceback to the caller."""
    logger.exception("request.unhandled_error", path=request.url.path)
    problem = {
        "type": "https://web-search-api.dev/problems/internal_error",
        "title": "Internal server error",
        "status": 500,
        "detail": "An unexpected error occurred.",
        "code": "internal_error",
        "instance": request.url.path,
    }
    return _problem_response(problem, 500)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop long-lived resources shared by all requests."""
    settings: Settings = app.state.settings
    logger.info("service.starting", service=settings.service_name, version=__version__)

    services = build_services(settings)
    app.state.services = services
    app.state.model_registry = services.registry
    app.state.summarizer = services.summarizer
    app.state.page_fetcher = services.page_fetcher
    app.state.search_router = services.search_router

    # The browser starts lazily on first use, so readiness reflects whether a
    # browser could be launched at all rather than whether one is running.
    app.state.browser_available = resolve_executable_path() is not None or settings.browser_headless

    try:
        yield
    finally:
        await services.aclose()
        logger.info("service.stopped", service=settings.service_name)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: Optional settings override, primarily for tests.
    """
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)

    app = FastAPI(
        title="web-search-api",
        version=__version__,
        summary=(
            "Scrape Google search results and web pages, then synthesise an "
            "executive summary with any of ~60 LLM providers."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.middleware("http")(build_auth_middleware(settings))
    app.middleware("http")(request_context_middleware)

    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    from app.api.routes import health, models, scrape, search, summarize

    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(search.router)
    app.include_router(scrape.router)
    app.include_router(summarize.router)
    return app


app = create_app()
