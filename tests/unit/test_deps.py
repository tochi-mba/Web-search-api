from dataclasses import dataclass, field
from types import SimpleNamespace

from app.api.deps import get_readiness_components, get_settings_dep


@dataclass
class FakeCatalog:
    models: list[str] = field(default_factory=list)
    providers: list[SimpleNamespace] = field(default_factory=list)


class FakeRegistry:
    def __init__(self, catalog):
        self._catalog = catalog

    async def catalog(self):
        return self._catalog


def make_request(**state):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def test_get_settings_dep_returns_settings():
    assert get_settings_dep().service_name


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
    catalog = FakeCatalog(
        models=["a", "b", "c"],
        providers=[
            SimpleNamespace(status="available"),
            SimpleNamespace(status="unauthorized"),
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
        make_request(browser_available=True, model_registry=FakeRegistry(FakeCatalog()))
    )
    llm = next(c for c in components if c.name == "llm")
    assert llm.ready is False
    assert llm.detail == "no reachable LLM provider"
