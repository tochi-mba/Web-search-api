"""FastAPI dependency providers.

Everything the routes need is resolved through this module so tests can swap in
fakes with ``app.dependency_overrides`` instead of monkeypatching internals.
"""

from __future__ import annotations

from fastapi import Request

from app.config import Settings, get_settings
from app.schemas.health import ReadinessComponent


def get_settings_dep() -> Settings:
    """Provide application settings."""
    return get_settings()


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
    checks.append(
        ReadinessComponent(
            name="llm",
            ready=ready,
            detail=(
                f"{len(catalog.models)} models across "
                f"{sum(1 for p in catalog.providers if p.status == 'available')} providers"
                if ready
                else "no reachable LLM provider"
            ),
        )
    )
    return checks
