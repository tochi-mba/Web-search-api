"""Registry tests.

The contract under test: GET /v1/models must show only models that are
reachable right now. A provider with a bad key or a dead endpoint disappears
from the catalogue instead of blowing up when someone tries to use it.
"""

import asyncio

import httpx
import pytest

from app.core.errors import (
    NotFoundError,
    ProviderUnavailableError,
    RateLimitedError,
    ValidationProblem,
)
from app.services.keyring.client import NO_AUTH
from app.services.llm.base import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ModelInfo,
    ProviderStatus,
)
from app.services.llm.registry import ModelRegistry, split_model_id


class StubProvider:
    def __init__(self, name, *, models=None, configured=True, error=None, delay=0.0):
        self.name = name
        self._models = models if models is not None else [f"{name}-model"]
        self._configured = configured
        self._error = error
        self._delay = delay
        self.chat_calls = []

    #: Stubs stand in for keyless providers unless a test says otherwise.
    requires_credential = False

    def is_configured(self):
        return self._configured

    async def list_models(self, auth=NO_AUTH):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return [ModelInfo.build(self.name, m) for m in self._models]

    async def chat(self, request, auth=NO_AUTH):
        self.chat_calls.append(request)
        return ChatResponse(text="summary", model=request.model, provider=self.name)


def registry(*providers, default_model="a:a-model", **kwargs):
    return ModelRegistry(
        list(providers),
        default_model=default_model,
        cache_ttl_seconds=kwargs.pop("cache_ttl_seconds", 300.0),
        **kwargs,
    )


def chat_request(model="ignored"):
    return ChatRequest(model=model, messages=[ChatMessage("user", "hi")])


# --- model id parsing ------------------------------------------------------ #


def test_split_takes_only_the_first_colon():
    assert split_model_id("ollama:llama3.1:8b") == ("ollama", "llama3.1:8b")


def test_split_handles_a_simple_id():
    assert split_model_id("anthropic:claude-opus-5") == ("anthropic", "claude-opus-5")


@pytest.mark.parametrize("bad", ["no-namespace", ":model", "provider:", ""])
def test_malformed_ids_are_rejected(bad):
    with pytest.raises(ValidationProblem, match="Malformed model id"):
        split_model_id(bad)


# --- probing --------------------------------------------------------------- #


async def test_available_provider_contributes_models():
    catalog = await registry(StubProvider("a", models=["m1", "m2"])).catalog()
    assert [m.id for m in catalog.models] == ["a:m1", "a:m2"]
    assert catalog.providers[0].status is ProviderStatus.AVAILABLE
    assert catalog.providers[0].model_count == 2


async def test_unconfigured_provider_is_reported_and_contributes_nothing():
    catalog = await registry(StubProvider("a", configured=False)).catalog()
    assert catalog.models == []
    assert catalog.providers[0].status is ProviderStatus.NOT_CONFIGURED


async def test_bad_credential_is_reported_as_unauthorized():
    provider = StubProvider(
        "a", error=ProviderUnavailableError("a rejected the credential", detail="bad key")
    )
    catalog = await registry(provider).catalog()
    assert catalog.providers[0].status is ProviderStatus.UNAUTHORIZED
    assert catalog.models == []


async def test_unreachable_provider_is_reported_as_unreachable():
    provider = StubProvider("a", error=ProviderUnavailableError("a unreachable", detail="no route"))
    catalog = await registry(provider).catalog()
    assert catalog.providers[0].status is ProviderStatus.UNREACHABLE


async def test_rate_limited_provider_is_reported_as_unreachable():
    provider = StubProvider("a", error=RateLimitedError("slow down", detail="429"))
    catalog = await registry(provider).catalog()
    assert catalog.providers[0].status is ProviderStatus.UNREACHABLE


async def test_unexpected_errors_do_not_break_the_catalogue():
    good = StubProvider("good", models=["m"])
    bad = StubProvider("bad", error=RuntimeError("something exploded"))
    catalog = await registry(good, bad).catalog()
    assert [m.id for m in catalog.models] == ["good:m"]
    assert {p.name: p.status for p in catalog.providers}["bad"] is ProviderStatus.UNREACHABLE


async def test_slow_providers_time_out_without_blocking_the_rest():
    slow = StubProvider("slow", delay=1.0)
    fast = StubProvider("fast", models=["m"])
    catalog = await registry(slow, fast, probe_timeout_seconds=0.05).catalog()
    assert [m.id for m in catalog.models] == ["fast:m"]
    statuses = {p.name: p.status for p in catalog.providers}
    assert statuses["slow"] is ProviderStatus.UNREACHABLE


async def test_models_and_providers_are_sorted_deterministically():
    catalog = await registry(
        StubProvider("zeta", models=["z"]), StubProvider("alpha", models=["a"])
    ).catalog()
    assert [m.id for m in catalog.models] == ["alpha:a", "zeta:z"]
    assert [p.name for p in catalog.providers] == ["alpha", "zeta"]


# --- caching --------------------------------------------------------------- #


class CountingProvider(StubProvider):
    """Records how many times it was probed."""

    def __init__(self, name, **kwargs):
        super().__init__(name, **kwargs)
        self.probe_count = 0

    async def list_models(self, auth=NO_AUTH):
        self.probe_count += 1
        return await super().list_models(auth)


async def test_catalog_is_cached():
    provider = CountingProvider("a", models=["m"])
    reg = registry(provider)
    await reg.catalog()
    await reg.catalog()
    assert provider.probe_count == 1


async def test_refresh_busts_the_cache():
    provider = CountingProvider("a", models=["m"])
    reg = registry(provider)
    await reg.catalog()
    await reg.catalog(refresh=True)
    assert provider.probe_count == 2


# --- resolution ------------------------------------------------------------ #


async def test_default_model_is_used_when_none_requested():
    reg = registry(StubProvider("a", models=["a-model"]), default_model="a:a-model")
    assert (await reg.resolve(None)).id == "a:a-model"


async def test_explicit_model_is_honoured():
    reg = registry(StubProvider("a", models=["m1", "m2"]))
    assert (await reg.resolve("a:m2")).id == "a:m2"


async def test_unknown_model_raises_not_found():
    reg = registry(StubProvider("a", models=["m1"]))
    with pytest.raises(NotFoundError, match="not available"):
        await reg.resolve("a:does-not-exist")


async def test_model_from_an_unavailable_provider_raises_not_found():
    reg = registry(
        StubProvider("a", models=["m1"]),
        StubProvider("b", error=ProviderUnavailableError("b rejected the credential")),
    )
    with pytest.raises(NotFoundError):
        await reg.resolve("b:anything")


async def test_unavailable_default_falls_back_to_something_that_works():
    reg = registry(StubProvider("b", models=["works"]), default_model="a:missing")
    assert (await reg.resolve(None)).id == "b:works"


async def test_nothing_available_raises_provider_unavailable():
    reg = registry(StubProvider("a", configured=False))
    with pytest.raises(ProviderUnavailableError, match="No LLM provider"):
        await reg.resolve(None)


async def test_malformed_requested_id_is_rejected():
    reg = registry(StubProvider("a", models=["m"]))
    with pytest.raises(ValidationProblem):
        await reg.resolve("garbage")


# --- completion ------------------------------------------------------------ #


async def test_complete_routes_to_the_right_provider():
    a = StubProvider("a", models=["m1"])
    b = StubProvider("b", models=["m2"])
    reg = registry(a, b)
    response = await reg.complete(chat_request(), model_id="b:m2")
    assert response.provider == "b"
    assert b.chat_calls[0].model == "m2"
    assert a.chat_calls == []


async def test_complete_strips_the_namespace_before_calling_the_provider():
    provider = StubProvider("ollama", models=["llama3.1:8b"])
    reg = registry(provider, default_model="ollama:llama3.1:8b")
    await reg.complete(chat_request(), model_id="ollama:llama3.1:8b")
    assert provider.chat_calls[0].model == "llama3.1:8b"


async def test_complete_preserves_request_parameters():
    provider = StubProvider("a", models=["m"])
    reg = registry(provider)
    await reg.complete(
        ChatRequest(
            model="ignored",
            messages=[ChatMessage("user", "hi")],
            system="be terse",
            max_output_tokens=123,
            temperature=0.9,
            effort="high",
            json_mode=True,
        ),
        model_id="a:m",
    )
    sent = provider.chat_calls[0]
    assert sent.system == "be terse"
    assert sent.max_output_tokens == 123
    assert sent.temperature == 0.9
    assert sent.effort == "high"
    assert sent.json_mode is True


def test_a_model_naming_an_unregistered_provider_is_not_found():
    """Catalogue entries always map back, so this is a ModelInfo from elsewhere."""
    reg = registry(StubProvider("a", models=["m"]))
    with pytest.raises(NotFoundError, match="Unknown provider"):
        reg.provider_for(ModelInfo.build("ghost", "m"))


def test_provider_names_are_exposed():
    assert set(registry(StubProvider("a"), StubProvider("b")).provider_names) == {"a", "b"}


def test_default_model_is_exposed():
    assert registry(StubProvider("a"), default_model="a:m").default_model == "a:m"


async def test_catalog_find_returns_none_for_unknown_ids():
    catalog = await registry(StubProvider("a", models=["m"])).catalog()
    assert catalog.find("nope:nope") is None
    assert catalog.find("a:m") is not None


async def test_disabled_providers_are_omitted_before_they_are_probed():
    allowed = CountingProvider("keep", models=["m"])
    blocked = CountingProvider("drop", models=["secret"])
    catalog = await registry(allowed, blocked).catalog(disabled_providers=["drop"])
    assert [m.id for m in catalog.models] == ["keep:m"]
    assert [p.name for p in catalog.providers] == ["keep"]
    assert blocked.probe_count == 0
    assert allowed.probe_count == 1


async def test_different_disabled_lists_do_not_share_a_catalogue_cache():
    provider = CountingProvider("keep", models=["m"])
    other = CountingProvider("drop", models=["x"])
    reg = registry(provider, other)
    first = await reg.catalog(disabled_providers=["drop"])
    second = await reg.catalog(disabled_providers=[])
    assert [p.name for p in first.providers] == ["keep"]
    assert {p.name for p in second.providers} == {"keep", "drop"}
    assert other.probe_count == 1


async def test_a_person_default_model_is_used_when_none_is_requested():
    reg = registry(StubProvider("a", models=["theirs"]), default_model="a:deployment")
    assert (await reg.resolve(None, default_model="a:theirs")).id == "a:theirs"


async def test_a_credentialed_model_without_a_profile_is_not_guessed():
    from app.core.errors import PreferencesUnavailableError
    from app.services.keyring.caller import Caller

    class Credentialed(StubProvider):
        requires_credential = True

    caller = Caller(account_id="acct", profile=None, user_token="token")
    local = StubProvider("ollama", models=["local"])
    provider = Credentialed("anthropic", models=["m"])
    reg = registry(local, provider, default_model="ollama:local")

    catalog = await reg.catalog(caller)
    assert [m.id for m in catalog.models] == ["ollama:local"]
    statuses = {p.name: p.status for p in catalog.providers}
    assert statuses["anthropic"] is ProviderStatus.NOT_CONFIGURED

    with pytest.raises(PreferencesUnavailableError):
        await reg.resolve("anthropic:m", caller)


async def test_a_credentialed_provider_without_a_caller_is_not_connected():
    class Credentialed(StubProvider):
        requires_credential = True

    catalog = await registry(Credentialed("anthropic", models=["m"])).catalog()
    assert catalog.models == []
    assert catalog.providers[0].status is ProviderStatus.NOT_CONFIGURED


async def test_without_keyring_a_named_profile_cannot_use_a_credentialed_provider():
    from app.services.keyring.caller import Caller

    class Credentialed(StubProvider):
        requires_credential = True

    caller = Caller(account_id="acct", profile="personal", user_token="token")
    catalog = await registry(Credentialed("anthropic", models=["m"])).catalog(caller)
    assert catalog.models == []
    assert catalog.providers[0].status is ProviderStatus.NOT_CONFIGURED


async def test_an_unconfigured_keyring_is_treated_as_absent():
    # Present but not usable: empty URL, same as never having been wired.
    from app.services.keyring.caller import Caller
    from app.services.keyring.client import KeyringClient

    class Credentialed(StubProvider):
        requires_credential = True

    caller = Caller(account_id="acct", profile="personal", user_token="token")
    async with httpx.AsyncClient() as http:
        keyring = KeyringClient(http, base_url="", service_token="svc")
        catalog = await registry(Credentialed("anthropic", models=["m"]), keyring=keyring).catalog(
            caller
        )
    assert catalog.models == []
    assert catalog.providers[0].status is ProviderStatus.NOT_CONFIGURED
