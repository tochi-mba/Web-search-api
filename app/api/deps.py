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
from app.core.errors import NotFoundError, ProviderUnavailableError
from app.schemas.health import ReadinessComponent

if TYPE_CHECKING:
    from app.services.fetch.page import PageFetcher
    from app.services.jobs.runner import JobRunner
    from app.services.keyring.caller import Caller
    from app.services.keyring.client import ResolvedAuth
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


def get_job_runner(request: Request) -> JobRunner:
    """Provide the background job runner."""
    from app.services.jobs.runner import JobRunner

    runner = _require(request, "job_runner", "job runner")
    assert isinstance(runner, JobRunner)  # noqa: S101 - built in the lifespan
    return runner


#: Where the end user's keyring token travels, matching keyring's own header.
USER_TOKEN_HEADER = "X-Keyring-User-Token"  # noqa: S105 - a header name

#: Which of the account's credential sets to draw from.
PROFILE_HEADER = "X-Keyring-Profile"


async def get_caller(request: Request) -> Caller | None:
    """Identify the end user this request is being made for.

    Returns ``None`` when no token is presented. That is legal: providers
    needing no credential stay usable, and the failure only arrives if the
    request actually needs a secret. Rejecting every anonymous request here
    would break local-only deployments that never touch keyring.
    """
    from app.services.keyring.caller import Caller

    token = request.headers.get(USER_TOKEN_HEADER)
    if not token:
        return None

    verifier = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        raise ProviderUnavailableError(
            "Keyring not configured",
            detail=(
                "A user token was presented but this deployment has no keyring "
                "configured to verify it against."
            ),
        )

    settings: Settings = request.app.state.settings
    account_id = await verifier.verify(token)
    profile = request.headers.get(PROFILE_HEADER) or settings.keyring_default_profile
    return Caller(account_id=account_id, profile=profile, user_token=token)


async def resolve_job_auth(
    registry: ModelRegistry, model_id: str | None, caller: Caller | None
) -> ResolvedAuth | None:
    """Resolve a background job's credential up front.

    A keyring user token lives minutes; a large batch can run longer. Carrying
    the token into the job would mean jobs failing halfway through with an auth
    error, so the credential is resolved while the token is fresh and the
    *result* is what the job carries.

    Returns ``None`` when nothing needed resolving, leaving the job to resolve
    lazily exactly as a synchronous request would.
    """
    from app.services.keyring.client import NotConnectedError

    if caller is None:
        return None
    model = await registry.resolve(model_id, caller)
    provider = registry.provider_for(model)
    try:
        return await registry.credential_for(provider, caller)
    except NotConnectedError as exc:
        raise NotFoundError(
            "No credential for this model",
            detail=(
                f"'{model.id}' needs a credential stored in keyring under service "
                f"'{provider.name}' for profile '{caller.profile}'."
            ),
        ) from exc


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
