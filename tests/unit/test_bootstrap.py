"""The composition root."""

import httpx
import pytest

from app.bootstrap import build_llm_providers, build_search_backends, build_services
from tests.conftest import make_settings


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


async def test_every_known_provider_is_constructed(client):
    providers, _ = build_llm_providers(make_settings(), client)
    names = {p.name for p in providers}
    assert "anthropic" in names
    assert "ollama" in names
    assert "openai" in names
    assert "groq" in names
    assert len(providers) > 40


async def test_providers_can_be_disabled(client):
    settings = make_settings(disabled_providers=("anthropic", "ollama", "groq"))
    providers, _ = build_llm_providers(settings, client)
    names = {p.name for p in providers}
    assert "anthropic" not in names
    assert "ollama" not in names
    assert "groq" not in names
    assert "openai" in names


async def test_base_url_overrides_are_applied(client):
    from app.services.llm.providers.ollama import OllamaProvider

    settings = make_settings(provider_base_urls={"ollama": "http://gpu-box:11434"})
    providers, _ = build_llm_providers(settings, client)
    ollama = next(p for p in providers if p.name == "ollama")
    assert isinstance(ollama, OllamaProvider)
    assert ollama._base_url == "http://gpu-box:11434"


class StubBrowser:
    """Stands in for a browser session; the backends only hold a reference."""

    async def render(self, url: str, *, wait_for_selector: str | None = None) -> str:
        return ""

    async def close(self) -> None:
        return None


async def test_search_backends_are_built_in_preference_order(client):
    backends = build_search_backends(make_settings(), client, StubBrowser())
    assert [b.name for b in backends] == ["google", "searxng", "serper"]


async def test_build_services_wires_everything():
    services = build_services(make_settings())
    try:
        assert services.registry.default_model == "anthropic:claude-opus-5"
        assert services.summarizer is not None
        assert services.page_fetcher is not None
        assert services.search_router is not None
        assert len(services.registry.provider_names) > 40
    finally:
        await services.aclose()


async def test_services_release_their_resources():
    services = build_services(make_settings())
    await services.aclose()
    assert services.http_client.is_closed


async def test_settings_flow_into_the_registry():
    services = build_services(
        make_settings(default_model="openai:gpt-4o", model_cache_ttl_seconds=42.0)
    )
    try:
        assert services.registry.default_model == "openai:gpt-4o"
        assert services.registry._cache.ttl_seconds == 42.0
    finally:
        await services.aclose()
