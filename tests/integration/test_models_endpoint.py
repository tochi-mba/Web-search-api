"""The catalogue must show only what is actually reachable."""

import pytest

from app.api import deps
from app.services.keyring.client import NO_AUTH
from app.services.llm.base import ChatResponse, ModelInfo
from app.services.llm.registry import ModelRegistry


class StubProvider:
    def __init__(self, name, models=(), configured=True, error=None):
        self.name = name
        self._models = list(models)
        self._configured = configured
        self._error = error

    requires_credential = False

    def is_configured(self):
        return self._configured

    async def list_models(self, auth=NO_AUTH):
        if self._error:
            raise self._error
        return [ModelInfo.build(self.name, m) for m in self._models]

    async def chat(self, request, auth=NO_AUTH):
        return ChatResponse(text="", model=request.model, provider=self.name)


@pytest.fixture
def registry_with(app):
    def _install(*providers, default_model="anthropic:claude-opus-5"):
        registry = ModelRegistry(
            list(providers), default_model=default_model, cache_ttl_seconds=300.0
        )
        app.dependency_overrides[deps.get_registry] = lambda: registry
        return registry

    return _install


async def test_lists_available_models(client, registry_with):
    registry_with(StubProvider("anthropic", ["claude-opus-5", "claude-haiku-4-5"]))
    body = (await client.get("/v1/models")).json()
    assert [m["id"] for m in body["models"]] == [
        "anthropic:claude-haiku-4-5",
        "anthropic:claude-opus-5",
    ]
    assert body["default_model"] == "anthropic:claude-opus-5"


async def test_capabilities_are_reported_per_model(client, registry_with):
    registry_with(StubProvider("anthropic", ["claude-opus-5", "claude-haiku-4-5"]))
    body = (await client.get("/v1/models")).json()
    by_id = {m["id"]: m for m in body["models"]}

    opus = by_id["anthropic:claude-opus-5"]
    assert opus["capabilities"]["supports_temperature"] is False
    assert opus["capabilities"]["reasoning"] == "adaptive_thinking"
    assert opus["context_window"] == 1_000_000

    haiku = by_id["anthropic:claude-haiku-4-5"]
    assert haiku["capabilities"]["supports_temperature"] is True
    assert haiku["capabilities"]["reasoning"] == "budget_tokens"
    assert haiku["context_window"] == 200_000


async def test_openai_reasoning_models_report_their_token_parameter(client, registry_with):
    registry_with(StubProvider("openai", ["o3", "gpt-4o"]))
    by_id = {m["id"]: m for m in (await client.get("/v1/models")).json()["models"]}
    assert by_id["openai:o3"]["capabilities"]["max_tokens_param"] == "max_completion_tokens"
    assert by_id["openai:gpt-4o"]["capabilities"]["max_tokens_param"] == "max_tokens"


async def test_a_provider_with_a_bad_key_contributes_no_models(client, registry_with):
    """The whole point of probing: a wrong key drops out instead of failing later."""
    from app.core.errors import ProviderUnavailableError

    registry_with(
        StubProvider("good", ["m1"]),
        StubProvider(
            "bad", error=ProviderUnavailableError("bad rejected the credential", detail="401")
        ),
    )
    body = (await client.get("/v1/models")).json()

    assert [m["id"] for m in body["models"]] == ["good:m1"]
    statuses = {p["name"]: p["status"] for p in body["providers"]}
    assert statuses == {"good": "available", "bad": "unauthorized"}


async def test_an_unreachable_provider_is_reported(client, registry_with):
    from app.core.errors import ProviderUnavailableError

    registry_with(StubProvider("down", error=ProviderUnavailableError("down unreachable")))
    body = (await client.get("/v1/models")).json()
    assert body["models"] == []
    assert body["providers"][0]["status"] == "unreachable"


async def test_an_unconfigured_provider_is_reported(client, registry_with):
    registry_with(StubProvider("nokey", configured=False))
    body = (await client.get("/v1/models")).json()
    assert body["providers"][0]["status"] == "not_configured"


async def test_provider_model_counts_are_reported(client, registry_with):
    registry_with(StubProvider("p", ["a", "b", "c"]))
    assert (await client.get("/v1/models")).json()["providers"][0]["model_count"] == 3


async def test_results_are_cached_between_calls(client, registry_with):
    class Counting(StubProvider):
        probes = 0

        async def list_models(self, auth=NO_AUTH):
            type(self).probes += 1
            return await super().list_models(auth)

    provider = Counting("p", ["m"])
    registry_with(provider)
    await client.get("/v1/models")
    await client.get("/v1/models")
    assert Counting.probes == 1


async def test_refresh_reprobes(client, registry_with):
    class Counting(StubProvider):
        probes = 0

        async def list_models(self, auth=NO_AUTH):
            type(self).probes += 1
            return await super().list_models(auth)

    registry_with(Counting("p", ["m"]))
    await client.get("/v1/models")
    await client.get("/v1/models", params={"refresh": "true"})
    assert Counting.probes == 2


async def test_empty_catalogue_is_a_valid_response(client, registry_with):
    registry_with(StubProvider("nokey", configured=False))
    response = await client.get("/v1/models")
    assert response.status_code == 200
    assert response.json()["models"] == []


async def test_uninitialised_registry_is_reported_cleanly(client, app):
    """A missing startup component must be a clear 503, not a 500."""
    app.dependency_overrides.pop(deps.get_registry, None)
    response = await client.get("/v1/models")
    assert response.status_code == 503
    assert response.json()["code"] == "provider_unavailable_error"
