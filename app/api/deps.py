"""FastAPI dependency providers.

Everything the routes need is resolved through this module, so tests can swap
in fakes with ``app.dependency_overrides`` rather than monkeypatching internals.
Long-lived objects are built once during application startup and read from
``app.state`` here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from app.config import Settings, get_settings
from app.core.errors import ProviderUnavailableError
from app.schemas.health import ReadinessComponent

if TYPE_CHECKING:
    from app.services.fetch.page import PageFetcher
    from app.services.llm.registry import ModelRegistry
    from app.services.llm.summarizer import Summarizer
    from app.services.search.router import SearchRouter


def get_settings_dep() -> Settings:
    """Provide application settings."""
    return get_settings()


def _require(request: Request, attribute: str, label: str) -> object:
    """Read a startup-built component from application state."""
    component = getattr(request.app.state, attribute, None)
    if component is None:
        raise ProviderUnavailableError(
            f"{label} unavailable",
            detail=f"The {label} was not initialised during startup.",
        )
    return component


def get_registry(request: Request) -> ModelRegistry:
    """Provide the model registry."""
    from app.services.llm.registry import ModelRegistry

    registry = _require(request, "model_registry", "model registry")
    assert isinstance(registry, ModelRegistry)  # noqa: S101 - built in the lifespan
    return registry


def get_summarizer(request: Request) -> Summarizer:
    """Provide the summariser."""
    from app.services.llm.summarizer import Summarizer

    summarizer = _require(request, "summarizer", "summariser")
    assert isinstance(summarizer, Summarizer)  # noqa: S101 - built in the lifespan
    return summarizer


def get_page_fetcher(request: Request) -> PageFetcher:
    """Provide the page fetcher."""
    from app.services.fetch.page import PageFetcher

    fetcher = _require(request, "page_fetcher", "page fetcher")
    assert isinstance(fetcher, PageFetcher)  # noqa: S101 - built in the lifespan
    return fetcher


def get_search_router(request: Request) -> SearchRouter:
    """Provide the search router."""
    from app.services.search.router import SearchRouter

    search_router = _require(request, "search_router", "search router")
    assert isinstance(search_router, SearchRouter)  # noqa: S101 - built in the lifespan
    return search_router


def get_max_concurrency(request: Request) -> int:
    """Provide the configured global concurrency limit."""
    settings: Settings = request.app.state.settings
    return settings.max_concurrency


async def get_readiness_components(request: Request) -> list[ReadinessComponent]:
    """Probe every dependency the service needs to do real work."""
    checks: list[ReadinessComponent] = []

    browser = getattr(request.app.state, "browser_available", None)
    checks.append(
        ReadinessComponent(
            name="browser",
            ready=bool(browser),
            detail="headless chromium ready" if browser else "browser not started",
        )
    )

    registry = getattr(request.app.state, "model_registry", None)
    if registry is None:
        checks.append(
            ReadinessComponent(name="llm", ready=False, detail="model registry not initialised")
        )
        return checks

    catalog = await registry.catalog()
    ready = bool(catalog.models)
    available = sum(1 for p in catalog.providers if p.is_available)
    checks.append(
        ReadinessComponent(
            name="llm",
            ready=ready,
            detail=(
                f"{len(catalog.models)} models across {available} providers"
                if ready
                else "no reachable LLM provider"
            ),
        )
    )
    return checks
