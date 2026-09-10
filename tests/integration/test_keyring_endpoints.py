"""End-to-end with a keyring: identity in the headers, credentials per caller."""

import httpx
import pytest
import respx

from app.api import deps
from app.main import create_app
from app.services.jobs.memory import InMemoryJobStore
from app.services.jobs.runner import JobRunner
from app.services.keyring.client import NO_AUTH, KeyringClient
from app.services.keyring.tokens import TokenVerifier
from app.services.llm.base import ChatResponse, ModelInfo
from app.services.llm.registry import ModelRegistry
from app.services.llm.summarizer import Summarizer
from tests.conftest import make_settings
from tests.fake_keyring import BASE_URL, FakeKeyring


class Provider:
    def __init__(self, name, *, requires_credential=True):
        self.name = name
        self.requires_credential = requires_credential
        self.seen = []

    def is_configured(self):
        return True

    async def list_models(self, auth=NO_AUTH):
        self.seen.append(auth)
        return [ModelInfo.build(self.name, "m")]

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
def providers():
    return [Provider("anthropic"), Provider("ollama", requires_credential=False)]


@pytest.fixture
def job_runner():
    """One runner for the whole test: a fresh one per request would lose jobs."""
    return JobRunner(InMemoryJobStore())


@pytest.fixture
def app(vault, http, providers, job_runner):
    settings = make_settings(
        keyring_base_url=BASE_URL,
        keyring_service_token="svc-token",
        keyring_service_name=vault.audience,
    )
    application = create_app(settings)
    keyring = KeyringClient(http, base_url=BASE_URL, service_token="svc-token")
    application.state.token_verifier = TokenVerifier(
        http, base_url=BASE_URL, audience=vault.audience
    )
    registry = ModelRegistry(
        providers,
        default_model="anthropic:m",
        cache_ttl_seconds=300.0,
        keyring=keyring,
    )
    application.dependency_overrides[deps.get_registry] = lambda: registry
    application.dependency_overrides[deps.get_summarizer] = lambda: Summarizer(registry)
    application.dependency_overrides[deps.get_job_runner] = lambda: job_runner
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def auth_headers(vault, account_id="acct-1", profile=None):
    headers = {"X-Keyring-User-Token": vault.token(account_id)}
    if profile:
        headers["X-Keyring-Profile"] = profile
    return headers


# --- the catalogue is per caller ------------------------------------------- #


@respx.mock
async def test_without_a_token_only_keyless_providers_appear(client, vault):
    vault.install(respx.mock)
    body = (await client.get("/v1/models")).json()
    assert [m["id"] for m in body["models"]] == ["ollama:m"]


@respx.mock
async def test_with_a_token_the_callers_providers_appear(client, vault):
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="acct-1")

    body = (await client.get("/v1/models", headers=auth_headers(vault))).json()
    assert sorted(m["id"] for m in body["models"]) == ["anthropic:m", "ollama:m"]


@respx.mock
async def test_two_accounts_see_different_catalogues(client, vault):
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="alice")

    alice = (await client.get("/v1/models", headers=auth_headers(vault, "alice"))).json()
    bob = (await client.get("/v1/models", headers=auth_headers(vault, "bob"))).json()

    assert "anthropic:m" in [m["id"] for m in alice["models"]]
    assert "anthropic:m" not in [m["id"] for m in bob["models"]]


@respx.mock
async def test_the_profile_header_selects_the_credential_set(client, vault):
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="acct-1", profile="work")

    default = (await client.get("/v1/models", headers=auth_headers(vault))).json()
    work = (await client.get("/v1/models", headers=auth_headers(vault, profile="work"))).json()

    assert "anthropic:m" not in [m["id"] for m in default["models"]]
    assert "anthropic:m" in [m["id"] for m in work["models"]]


# --- token rejection ------------------------------------------------------- #


@respx.mock
async def test_a_bad_token_is_rejected(client, vault):
    vault.install(respx.mock)
    response = await client.get("/v1/models", headers={"X-Keyring-User-Token": "not-a-real-token"})
    assert response.status_code == 401
    assert response.json()["code"] == "auth_error"


@respx.mock
async def test_a_token_for_another_service_is_rejected(client, vault):
    vault.install(respx.mock)
    stranger = FakeKeyring(key_id=vault.key_id)
    response = await client.get(
        "/v1/models",
        headers={"X-Keyring-User-Token": vault.token(audience="some-other-service")},
    )
    assert response.status_code == 401
    assert stranger is not None


async def test_a_token_without_keyring_configured_is_reported(vault):
    """Presenting a token to a deployment that cannot verify it must be clear."""
    application = create_app(make_settings())
    application.dependency_overrides[deps.get_registry] = lambda: ModelRegistry(
        [], default_model="anthropic:m", cache_ttl_seconds=300.0
    )
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/v1/models", headers={"X-Keyring-User-Token": vault.token()})
    assert response.status_code == 503
    assert "no keyring configured" in response.json()["detail"]


# --- credentials reach the provider ---------------------------------------- #


@respx.mock
async def test_the_callers_credential_reaches_the_provider(client, vault, providers):
    vault.install(respx.mock)
    vault.connect("anthropic", account_id="acct-1", headers={"x-api-key": "sk-caller"})

    await client.post(
        "/v1/summarize",
        json={"text": "some text", "model": "anthropic:m"},
        headers=auth_headers(vault),
    )
    anthropic = providers[0]
    assert anthropic.seen[-1].headers == {"x-api-key": "sk-caller"}


@respx.mock
async def test_asking_for_a_model_you_have_no_credential_for(client, vault):
    vault.install(respx.mock)
    response = await client.post(
        "/v1/summarize",
        json={"text": "some text", "model": "anthropic:m"},
        headers=auth_headers(vault),
    )
    assert response.status_code == 404
    assert "not available" in response.json()["detail"]


# --- background jobs ------------------------------------------------------- #


@respx.mock
async def test_a_background_job_carries_resolved_auth_not_the_token(client, vault, providers):
    """Tokens live minutes; a job must not depend on one still being valid."""
    import asyncio

    vault.install(respx.mock)
    vault.connect("anthropic", account_id="acct-1", headers={"x-api-key": "sk-job"})

    accepted = await client.post(
        "/v1/summarize",
        json={"text": "some text", "model": "anthropic:m", "async": True},
        headers=auth_headers(vault),
    )
    assert accepted.status_code == 202

    job_id = accepted.json()["job_id"]
    for _ in range(200):
        body = (await client.get(f"/v1/jobs/{job_id}")).json()
        if body["status"] in ("succeeded", "failed"):
            break
        await asyncio.sleep(0)

    assert body["status"] == "succeeded"
    assert providers[0].seen[-1].headers == {"x-api-key": "sk-job"}


@respx.mock
async def test_submitting_a_job_without_a_credential_fails_immediately(client, vault):
    """Better to refuse at submit than to hand back a job id that cannot work."""
    vault.install(respx.mock)
    response = await client.post(
        "/v1/summarize",
        json={"text": "some text", "model": "anthropic:m", "async": True},
        headers=auth_headers(vault),
    )
    assert response.status_code == 404


@respx.mock
async def test_a_job_whose_credential_vanishes_before_submit(client, vault, app):
    """Submitting must refuse rather than hand back a job id that cannot work."""
    from app.services.llm.registry import ModelRegistry as _Registry

    vault.install(respx.mock)
    vault.connect("anthropic", account_id="acct-1")
    registry: _Registry = app.dependency_overrides[deps.get_registry]()
    await registry.catalog(
        __import__("app.services.keyring.caller", fromlist=["Caller"]).Caller(
            account_id="acct-1", profile="personal", user_token=vault.token()
        )
    )
    vault.credentials.clear()

    response = await client.post(
        "/v1/summarize",
        json={"text": "x", "model": "anthropic:m", "async": True},
        headers=auth_headers(vault),
    )
    assert response.status_code == 404
    assert "stored in keyring" in response.json()["detail"]
