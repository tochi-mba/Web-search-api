"""The dependency providers themselves.

Endpoint tests override these, so they need direct coverage - including the
path where a startup component is missing, which must be a clean 503 rather
than an AttributeError.
"""

from types import SimpleNamespace

import pytest

from app.api.deps import (
    get_job_runner,
    get_max_concurrency,
    get_page_fetcher,
    get_preference_source,
    get_readiness_components,
    get_registry,
    get_search_router,
    get_settings_dep,
    get_summarizer,
)
from app.core.errors import ProviderUnavailableError
from app.services.llm.base import ModelInfo, ProviderHealth, ProviderStatus
from app.services.llm.registry import ModelCatalog
from tests.conftest import make_settings


def make_request(**state):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


class FakeRegistry:
    def __init__(self, catalog):
        self._catalog = catalog

    async def catalog(self, caller=None, *, refresh=False, disabled_providers=()):
        return self._catalog


class UnreachableSettings:
    """A preference source standing in for settings-api being down.

    Satisfies the real protocol, so a change to it fails here rather than passing quietly.
    """

    async def for_token(self, user_token=None, /):
        return self

    async def aclose(self):
        return None

    def require_disabled_providers(self):
        from app.core.errors import PreferencesUnavailableError

        raise PreferencesUnavailableError("the disabled-provider list is unknown")


def test_get_settings_dep_returns_settings():
    assert get_settings_dep().service_name


def test_max_concurrency_comes_from_settings():
    request = make_request(settings=make_settings(max_concurrency=17))
    assert get_max_concurrency(request) == 17


# --- missing components ---------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider", "label"),
    [
        (get_registry, "model registry"),
        (get_summarizer, "summariser"),
        (get_page_fetcher, "page fetcher"),
        (get_search_router, "search router"),
        (get_job_runner, "job runner"),
    ],
)
def test_missing_components_raise_a_clean_service_error(provider, label):
    with pytest.raises(ProviderUnavailableError, match=label):
        provider(make_request())


# --- present components ---------------------------------------------------- #


def test_registry_is_returned_when_present():
    from app.services.llm.registry import ModelRegistry

    registry = ModelRegistry([], default_model="a:b", cache_ttl_seconds=1.0)
    assert get_registry(make_request(model_registry=registry)) is registry


def test_summarizer_is_returned_when_present():
    from app.services.llm.registry import ModelRegistry
    from app.services.llm.summarizer import Summarizer

    summarizer = Summarizer(ModelRegistry([], default_model="a:b", cache_ttl_seconds=1.0))
    assert get_summarizer(make_request(summarizer=summarizer)) is summarizer


def test_page_fetcher_is_returned_when_present():
    from app.services.fetch.page import PageFetcher

    fetcher = PageFetcher(object())  # type: ignore[arg-type]
    assert get_page_fetcher(make_request(page_fetcher=fetcher)) is fetcher


def test_job_runner_is_returned_when_present():
    from app.services.jobs.memory import InMemoryJobStore
    from app.services.jobs.runner import JobRunner

    runner = JobRunner(InMemoryJobStore())
    assert get_job_runner(make_request(job_runner=runner)) is runner


def test_preference_source_is_returned_when_present():
    from app.services.preferences import DeploymentPreferences

    source = DeploymentPreferences(make_settings())
    assert (
        get_preference_source(make_request(preferences=source, settings=make_settings())) is source
    )


def test_preference_source_falls_back_to_the_configuration():
    from app.services.preferences import DeploymentPreferences

    settings = make_settings()
    source = get_preference_source(make_request(settings=settings))
    assert isinstance(source, DeploymentPreferences)


def test_search_router_is_returned_when_present():
    from app.services.search.router import SearchRouter

    router = SearchRouter([])
    assert get_search_router(make_request(search_router=router)) is router


# --- readiness ------------------------------------------------------------- #


async def test_missing_registry_reports_not_initialised():
    components = await get_readiness_components(make_request(browser_available=True))
    llm = next(c for c in components if c.name == "llm")
    assert llm.ready is False
    assert "not initialised" in llm.detail


async def test_browser_component_reflects_state():
    ready = await get_readiness_components(make_request(browser_available=True))
    assert next(c for c in ready if c.name == "browser").ready is True

    not_ready = await get_readiness_components(make_request(browser_available=False))
    assert next(c for c in not_ready if c.name == "browser").ready is False


async def test_registry_with_models_is_ready():
    catalog = ModelCatalog(
        models=[ModelInfo.build("p", f"m{i}") for i in range(3)],
        providers=[
            ProviderHealth(name="up", status=ProviderStatus.AVAILABLE),
            ProviderHealth(name="down", status=ProviderStatus.UNAUTHORIZED),
        ],
    )
    components = await get_readiness_components(
        make_request(
            browser_available=True,
            model_registry=FakeRegistry(catalog),
            settings=make_settings(),
        )
    )
    llm = next(c for c in components if c.name == "llm")
    assert llm.ready is True
    assert llm.detail == (
        "2 providers configured, 1 reachable without a credential, 3 models listed anonymously"
    )


async def test_a_registry_with_no_provider_configured_is_not_ready():
    components = await get_readiness_components(
        make_request(
            browser_available=True,
            model_registry=FakeRegistry(ModelCatalog()),
            settings=make_settings(),
        )
    )
    llm = next(c for c in components if c.name == "llm")
    assert llm.ready is False
    assert llm.detail == "no provider configured"


async def test_a_provider_that_needs_a_credential_still_makes_it_ready():
    # The probe carries no credential, so a cloud provider answers "unauthorized". That is
    # not this service being unready: every caller who brings a token is served. Requiring a
    # model here reported a healthy cloud-only deployment as permanently unready.
    catalog = ModelCatalog(
        models=[],
        providers=[ProviderHealth(name="anthropic", status=ProviderStatus.UNAUTHORIZED)],
    )

    components = await get_readiness_components(
        make_request(
            browser_available=True,
            model_registry=FakeRegistry(catalog),
            settings=make_settings(),
        )
    )

    llm = next(c for c in components if c.name == "llm")
    assert llm.ready is True
    assert "1 providers configured" in llm.detail


async def test_a_provider_that_is_not_configured_does_not_count():
    catalog = ModelCatalog(
        models=[],
        providers=[ProviderHealth(name="ollama", status=ProviderStatus.NOT_CONFIGURED)],
    )

    components = await get_readiness_components(
        make_request(
            browser_available=True,
            model_registry=FakeRegistry(catalog),
            settings=make_settings(),
        )
    )

    assert next(c for c in components if c.name == "llm").ready is False


async def test_settings_api_being_unreachable_does_not_fail_the_probe():
    # Readiness is a fact about this process. A probe that raised because settings-api was
    # down would take this service out of rotation for somebody else's outage.
    catalog = ModelCatalog(
        models=[],
        providers=[ProviderHealth(name="anthropic", status=ProviderStatus.AVAILABLE)],
    )

    components = await get_readiness_components(
        make_request(
            browser_available=True,
            model_registry=FakeRegistry(catalog),
            settings=make_settings(),
            preferences=UnreachableSettings(),
        )
    )

    assert next(c for c in components if c.name == "llm").ready is True
