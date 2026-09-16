"""FastAPI dependency providers.

Everything the routes need is resolved through this module, so tests can swap
in fakes with ``app.dependency_overrides`` rather than monkeypatching internals.
Long-lived objects are built once during application startup and read from
``app.state`` here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

from app.config import Settings, get_settings
from app.core.errors import AuthError, NotFoundError, ProviderUnavailableError
from app.core.logging import get_logger
from app.schemas.health import ReadinessComponent
from app.services.keyring.caller import Caller
from app.services.preferences import Preferences

if TYPE_CHECKING:
    from app.services.fetch.page import PageFetcher
    from app.services.jobs.runner import JobRunner
    from app.services.keyring.client import ResolvedAuth
    from app.services.llm.registry import ModelRegistry
    from app.services.llm.summarizer import Summarizer
    from app.services.preferences import PreferenceSource
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


def get_preference_source(request: Request) -> PreferenceSource:
    """Provide the per-person settings source."""
    from app.services.preferences import DeploymentPreferences, PreferenceSource

    source = getattr(request.app.state, "preferences", None)
    if source is None:
        # Tests that never start the lifespan still have settings on the app.
        settings: Settings = request.app.state.settings
        source = DeploymentPreferences(settings)
        request.app.state.preferences = source
    assert isinstance(source, PreferenceSource)  # noqa: S101 - built in the lifespan
    return source


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
    from app.services.keyring.tokens import BAD_TOKEN

    token = request.headers.get(USER_TOKEN_HEADER)
    authorization = request.headers.get("Authorization")
    if authorization is not None:
        scheme, _, bearer = authorization.partition(" ")
        if scheme.lower() != "bearer" or not bearer or (token and token != bearer):
            raise AuthError(BAD_TOKEN)
        token = bearer
    elif token:
        get_logger(__name__).info("legacy_user_token_header", replacement="Authorization: Bearer")
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

    account_id = await verifier.verify(token)
    source = get_preference_source(request)
    preferences = await source.for_token(token)
    request.state.preferences_resolved = preferences
    requested_profile = request.headers.get(PROFILE_HEADER)
    profile = requested_profile if requested_profile else preferences.default_profile
    return Caller(account_id=account_id, profile=profile, user_token=token)


async def get_preferences(
    request: Request,
    caller: Annotated[Caller | None, Depends(get_caller)],
) -> Preferences:
    """The preferences of whoever this request is for, or the configuration."""
    cached = getattr(request.state, "preferences_resolved", None)
    if isinstance(cached, Preferences):
        return cached
    source = get_preference_source(request)
    token = caller.user_token if caller is not None else None
    preferences = await source.for_token(token)
    request.state.preferences_resolved = preferences
    return preferences


async def resolve_job_auth(
    registry: ModelRegistry,
    model_id: str | None,
    caller: Caller | None,
    preferences: Preferences,
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
    model = await registry.resolve(
        model_id,
        caller,
        disabled_providers=preferences.require_disabled_providers(),
        default_model=preferences.default_model,
    )
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

    from app.core.errors import PreferencesUnavailableError
    from app.services.llm.base import ProviderStatus

    # This probe acts for nobody, so it carries no credential and applies no person's
    # disabled list. settings-api being unreachable must not make it raise: readiness is a
    # fact about this process, and a probe that failed would report the wrong service down.
    try:
        preferences = await get_preference_source(request).for_token(None)
        disabled: frozenset[str] = preferences.require_disabled_providers()
    except PreferencesUnavailableError:
        disabled = frozenset()

    catalog = await registry.catalog(disabled_providers=disabled)
    configured = [
        provider
        for provider in catalog.providers
        if provider.status is not ProviderStatus.NOT_CONFIGURED
    ]
    available = sum(1 for provider in catalog.providers if provider.is_available)
    # Ready means this service can serve a caller who brings a credential -- not that an
    # anonymous probe reached a model. Most providers need a per-caller credential from
    # keyring, so requiring a model here reported a healthy, cloud-only deployment as
    # permanently unready while every real caller was being served.
    checks.append(
        ReadinessComponent(
            name="llm",
            ready=bool(configured),
            detail=(
                f"{len(configured)} providers configured, {available} reachable without a "
                f"credential, {len(catalog.models)} models listed anonymously"
                if configured
                else "no provider configured"
            ),
        )
    )
    return checks
