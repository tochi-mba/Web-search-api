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
    """Providers hold no credentials, so all of them are always built."""
    providers = build_llm_providers(make_settings(), client)
    names = {p.name for p in providers}
    assert "anthropic" in names
    assert "ollama" in names
    assert "openai" in names
    assert "groq" in names
    assert len(providers) > 40


async def test_disabled_providers_are_still_constructed(client):
    """Per-caller filtering cannot happen if the provider was never built."""
    settings = make_settings(disabled_providers=("anthropic", "ollama", "groq"))
    providers = build_llm_providers(settings, client)
    names = {p.name for p in providers}
    assert "anthropic" in names
    assert "ollama" in names
    assert "groq" in names
    assert "openai" in names


async def test_base_url_overrides_are_applied(client):
    from app.services.llm.providers.ollama import OllamaProvider

    settings = make_settings(provider_base_urls={"ollama": "http://gpu-box:11434"})
    providers = build_llm_providers(settings, client)
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
        assert services.registry._cache_ttl == 42.0
    finally:
        await services.aclose()


async def test_no_verifier_is_built_without_keyring():
    """A token presented to a deployment with no keyring is refused as unconfigured."""
    services = build_services(make_settings())
    try:
        assert services.token_verifier is None
    finally:
        await services.aclose()


async def test_a_configured_verifier_is_released_with_everything_else():
    services = build_services(
        make_settings(
            keyring_base_url="http://127.0.0.1:8001",
            keyring_service_token="svc-token-0123456789abcdef0123456789",
        )
    )
    assert services.token_verifier is not None
    await services.aclose()
    assert services.token_verifier._jwks._client.is_closed


async def test_without_settings_api_everybody_gets_the_configuration():
    from app.services.preferences import DeploymentPreferences

    services = build_services(make_settings())
    try:
        assert isinstance(services.preferences, DeploymentPreferences)
    finally:
        await services.aclose()


async def test_a_settings_client_is_not_asked_at_startup_and_is_closed():
    from app.services.preferences import SettingsApiPreferences, build_preference_source

    class RecordingClient:
        def __init__(self) -> None:
            self.closed = False
            self.resolves = 0

        async def resolve(self, namespace: str, *, user_token: str):
            self.resolves += 1
            raise AssertionError("must not fetch settings at startup")

        async def set(self, namespace: str, key: str, value: object, *, user_token: str) -> int:
            raise AssertionError("must not write settings at startup")

        async def aclose(self) -> None:
            self.closed = True

    client = RecordingClient()
    source = build_preference_source(make_settings(), client=client)
    services = build_services(make_settings(), preferences=source)
    try:
        assert isinstance(services.preferences, SettingsApiPreferences)
        assert client.resolves == 0
    finally:
        await services.aclose()
    assert client.closed
