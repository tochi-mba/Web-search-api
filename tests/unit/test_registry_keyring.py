"""Per-caller catalogues.

Credentials belong to people, so the catalogue does too. The test that matters
most here is the isolation one: one caller must never see another's providers.
"""

import httpx
import pytest
import respx

from app.core.errors import NotFoundError, ProviderUnavailableError
from app.services.keyring.caller import Caller
from app.services.keyring.client import NO_AUTH, KeyringClient, ResolvedAuth
from app.services.llm.base import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ModelInfo,
    ProviderStatus,
)
from app.services.llm.registry import ModelRegistry
from tests.fake_keyring import BASE_URL, FakeKeyring


class Provider:
    """A provider that records the credential it was handed."""

    def __init__(self, name, *, models=("m",), requires_credential=True):
        self.name = name
        self.requires_credential = requires_credential
        self._models = list(models)
        self.seen: list[ResolvedAuth] = []

    def is_configured(self):
        return True

    async def list_models(self, auth=NO_AUTH):
        self.seen.append(auth)
        return [ModelInfo.build(self.name, m) for m in self._models]

    async def chat(self, request, auth=NO_AUTH):
        self.seen.append(auth)
        return ChatResponse(text="ok", model=request.model, provider=self.name)


@pytest.fixture
def vault():
    return FakeKeyring()


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def keyring(http):
    return KeyringClient(http, base_url=BASE_URL, service_token="svc")


def caller_for(vault, account_id="acct-1", profile="personal"):
    return Caller(account_id=account_id, profile=profile, user_token=vault.token(account_id))


def build(providers, keyring=None, **kwargs):
    return ModelRegistry(
        list(providers),
        default_model=kwargs.pop("default_model", "anthropic:m"),
        cache_ttl_seconds=kwargs.pop("cache_ttl_seconds", 300.0),
        keyring=keyring,
        **kwargs,
    )


def chat_request():
    return ChatRequest(model="ignored", messages=[ChatMessage("user", "hi")])


# --- isolation ------------------------------------------------------------- #


@respx.mock
async def test_one_caller_never_sees_anothers_providers(keyring, vault):
    """The security property this whole design exists to protect."""
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="alice")
    vault.connect("groq", account_id="bob")

    registry = build([Provider("anthropic"), Provider("groq")], keyring)

    alice = await registry.catalog(caller_for(vault, "alice"))
    bob = await registry.catalog(caller_for(vault, "bob"))

    assert [m.id for m in alice.models] == ["anthropic:m"]
    assert [m.id for m in bob.models] == ["groq:m"]


@respx.mock
async def test_each_callers_own_credential_reaches_their_provider(keyring, vault):
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="alice", headers={"x-api-key": "alice-key"})
    vault.connect("anthropic", account_id="bob", headers={"x-api-key": "bob-key"})

    provider = Provider("anthropic")
    registry = build([provider], keyring)

    await registry.catalog(caller_for(vault, "alice"))
    await registry.catalog(caller_for(vault, "bob"))

    assert [a.headers["x-api-key"] for a in provider.seen] == ["alice-key", "bob-key"]


@respx.mock
async def test_the_same_account_on_different_profiles_is_cached_apart(keyring, vault):
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="alice", profile="personal")
    vault.connect("groq", account_id="alice", profile="work")

    registry = build([Provider("anthropic"), Provider("groq")], keyring)

    personal = await registry.catalog(caller_for(vault, "alice", "personal"))
    work = await registry.catalog(caller_for(vault, "alice", "work"))

    assert [m.id for m in personal.models] == ["anthropic:m"]
    assert [m.id for m in work.models] == ["groq:m"]


@respx.mock
async def test_a_catalogue_is_cached_per_caller_not_per_token(keyring, vault):
    """Tokens rotate every few minutes; the cache must survive that."""
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="alice")
    provider = Provider("anthropic")
    registry = build([provider], keyring)

    await registry.catalog(caller_for(vault, "alice"))
    # A fresh token for the same account, as a client would send minutes later.
    await registry.catalog(caller_for(vault, "alice"))

    assert len(provider.seen) == 1


# --- what a caller can see ------------------------------------------------- #


@respx.mock
async def test_unconnected_providers_are_reported_but_contribute_nothing(keyring, vault):
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="alice")

    registry = build([Provider("anthropic"), Provider("groq")], keyring)
    catalog = await registry.catalog(caller_for(vault, "alice"))

    statuses = {p.name: p.status for p in catalog.providers}
    assert statuses["anthropic"] is ProviderStatus.AVAILABLE
    assert statuses["groq"] is ProviderStatus.NOT_CONFIGURED
    assert "No credential for this caller" in next(
        p.detail for p in catalog.providers if p.name == "groq"
    )


@respx.mock
async def test_an_unconnected_provider_is_never_called(keyring, vault):
    """The short-circuit that keeps a fifty-provider sweep cheap."""
    vault.install(respx.mock)
    provider = Provider("groq")
    registry = build([provider], keyring)

    await registry.catalog(caller_for(vault, "alice"))
    assert provider.seen == []


@respx.mock
async def test_a_rejected_credential_is_reported_as_unauthorized(keyring, vault):
    vault.install(respx.mock)
    vault.fail("anthropic", 503)

    registry = build([Provider("anthropic")], keyring)
    catalog = await registry.catalog(caller_for(vault, "alice"))

    assert catalog.providers[0].status is ProviderStatus.UNAUTHORIZED


@respx.mock
async def test_keyless_providers_need_no_caller_at_all(keyring, vault):
    """A local-only deployment must work without keyring involved."""
    vault.install(respx.mock)
    registry = build(
        [Provider("ollama", requires_credential=False), Provider("anthropic")], keyring
    )
    catalog = await registry.catalog(None)

    assert [m.id for m in catalog.models] == ["ollama:m"]
    statuses = {p.name: p.status for p in catalog.providers}
    assert statuses["ollama"] is ProviderStatus.AVAILABLE
    assert statuses["anthropic"] is ProviderStatus.NOT_CONFIGURED


async def test_without_keyring_only_keyless_providers_are_usable():
    registry = build([Provider("ollama", requires_credential=False), Provider("anthropic")])
    catalog = await registry.catalog(None)
    assert [m.id for m in catalog.models] == ["ollama:m"]


# --- completion ------------------------------------------------------------ #


@respx.mock
async def test_completion_uses_the_callers_credential(keyring, vault):
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="alice", headers={"x-api-key": "alice-key"})

    provider = Provider("anthropic")
    registry = build([provider], keyring, default_model="anthropic:m")
    caller = caller_for(vault, "alice")

    await registry.complete(chat_request(), model_id="anthropic:m", caller=caller)
    assert provider.seen[-1].headers == {"x-api-key": "alice-key"}


@respx.mock
async def test_a_pre_resolved_credential_is_used_as_is(keyring, vault):
    """Background jobs resolve early and hand the result over."""
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="alice")
    provider = Provider("anthropic")
    registry = build([provider], keyring, default_model="anthropic:m")
    caller = caller_for(vault, "alice")
    await registry.catalog(caller)

    provider.seen.clear()
    await registry.complete(
        chat_request(),
        model_id="anthropic:m",
        caller=caller,
        auth=ResolvedAuth(headers={"x-api-key": "resolved-earlier"}),
    )
    assert provider.seen[-1].headers == {"x-api-key": "resolved-earlier"}


@respx.mock
async def test_completing_without_a_credential_names_the_fix(keyring, vault):
    vault.install(respx.mock)
    vault.connect("ollama", account_id="alice")
    registry = build(
        [Provider("ollama", requires_credential=False), Provider("anthropic")],
        keyring,
        default_model="ollama:m",
    )
    caller = caller_for(vault, "alice")

    with pytest.raises(NotFoundError, match="not available"):
        await registry.complete(chat_request(), model_id="anthropic:m", caller=caller)


async def test_nothing_available_at_all_points_at_the_catalogue():
    registry = build([Provider("anthropic")])
    with pytest.raises(ProviderUnavailableError, match="keyring"):
        await registry.resolve(None, None)


# --- cache bounds ---------------------------------------------------------- #


@respx.mock
async def test_the_caller_cache_is_bounded(keyring, vault):
    """A stranger per request must not grow the cache without limit."""
    vault.install(respx.mock)
    registry = build([Provider("anthropic")], keyring, max_cached_callers=3)

    for i in range(10):
        await registry.catalog(caller_for(vault, f"acct-{i}"))

    assert len(registry._caches) <= 3


@respx.mock
async def test_a_credential_revoked_after_the_catalogue_was_built(keyring, vault):
    """A real race: the catalogue is cached, the credential is not.

    Someone disconnects the service in keyring while a session is open. The
    model is still in the cached catalogue, so the failure surfaces here rather
    than at probe time, and it must name the fix.
    """
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="alice")
    registry = build([Provider("anthropic")], keyring, default_model="anthropic:m")
    caller = caller_for(vault, "alice")

    await registry.catalog(caller)
    vault.credentials.clear()

    with pytest.raises(NotFoundError, match="stored in keyring"):
        await registry.complete(chat_request(), model_id="anthropic:m", caller=caller)
