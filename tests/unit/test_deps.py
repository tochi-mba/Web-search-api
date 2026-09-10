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

    async def catalog(self):
        return self._catalog


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
        make_request(browser_available=True, model_registry=FakeRegistry(catalog))
    )
    llm = next(c for c in components if c.name == "llm")
    assert llm.ready is True
    assert llm.detail == "3 models across 1 providers"


async def test_registry_without_models_is_not_ready():
    components = await get_readiness_components(
        make_request(browser_available=True, model_registry=FakeRegistry(ModelCatalog()))
    )
    llm = next(c for c in components if c.name == "llm")
    assert llm.ready is False
    assert llm.detail == "no reachable LLM provider"
