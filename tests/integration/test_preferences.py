"""Per-person settings over HTTP: two accounts see different catalogues.

settings-api is its shared fake here, wired in through the composition root the
way the real client is. Outages refuse only operations that need a refused key.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from settings_client.models import Value
from settings_client.testing import FakeSettingsClient

from app.api import deps
from app.main import create_app
from app.services.fetch.page import FetchedPage
from app.services.jobs.memory import InMemoryJobStore
from app.services.jobs.runner import JobRunner
from app.services.keyring.client import NO_AUTH
from app.services.keyring.tokens import TokenVerifier
from app.services.llm.base import ChatResponse, ModelInfo
from app.services.llm.registry import ModelRegistry
from app.services.llm.summarizer import Summarizer
from app.services.text.extractor import ExtractedContent
from tests.conftest import make_settings
from tests.fake_keyring import BASE_URL, ISSUER, FakeKeyring

SERVICE_TOKEN = "svc-token-0123456789abcdef0123456789"


class Provider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.requires_credential = False

    def is_configured(self) -> bool:
        return True

    async def list_models(self, auth: object = NO_AUTH) -> list[ModelInfo]:
        return [ModelInfo.build(self.name, "m")]

    async def chat(self, request: object, auth: object = NO_AUTH) -> ChatResponse:
        return ChatResponse(text="ok", model="m", provider=self.name)


class RecordingPages:
    async def fetch(self, url: str, *, render: str = "auto") -> FetchedPage:
        return FetchedPage(
            content=ExtractedContent(
                url=url, title="A Page", author=None, published=None, text="hello from the page"
            ),
            final_url=url,
            rendered=False,
        )


class PerTokenFake:
    """One fake per user token, so two accounts can hold different values."""

    def __init__(self) -> None:
        self._clients: dict[str, FakeSettingsClient] = {}
        self.unavailable = False
        self.rejects: dict[str, tuple[int, str]] = {}

    def seed(self, token: str, namespace: str, values: dict[str, Any]) -> None:
        self._clients.setdefault(token, FakeSettingsClient()).seed(namespace, values)

    async def resolve(self, namespace: str, *, user_token: str) -> object:
        if self.unavailable:
            from settings_client import SettingsUnavailable

            raise SettingsUnavailable("settings-api could not be reached")
        if namespace in self.rejects:
            from settings_client import SettingsRejected

            status, detail = self.rejects[namespace]
            raise SettingsRejected(status, detail)
        return await self._clients.setdefault(user_token, FakeSettingsClient()).resolve(
            namespace, user_token=user_token
        )

    async def set(self, namespace: str, key: str, value: Value, *, user_token: str) -> int:
        return await self._clients.setdefault(user_token, FakeSettingsClient()).set(
            namespace, key, value, user_token=user_token
        )

    async def aclose(self) -> None:
        return


@pytest.fixture
def vault() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def chosen() -> PerTokenFake:
    return PerTokenFake()


@pytest.fixture
def providers() -> list[Provider]:
    return [Provider("anthropic"), Provider("openai")]


@pytest.fixture
async def app(vault: FakeKeyring, chosen: PerTokenFake, providers: list[Provider]):
    settings = make_settings(
        keyring_base_url=BASE_URL,
        keyring_service_token=SERVICE_TOKEN,
        keyring_service_name=vault.audience,
        keyring_issuer=ISSUER,
        disabled_providers=(),
    )
    application = create_app(settings, settings_client=chosen)
    application.state.token_verifier = TokenVerifier(
        base_url=BASE_URL, issuer=ISSUER, audience=vault.audience
    )
    registry = ModelRegistry(providers, default_model="anthropic:m", cache_ttl_seconds=300.0)
    job_runner = JobRunner(InMemoryJobStore())
    application.dependency_overrides[deps.get_registry] = lambda: registry
    application.dependency_overrides[deps.get_summarizer] = lambda: Summarizer(registry)
    application.dependency_overrides[deps.get_page_fetcher] = lambda: RecordingPages()
    application.dependency_overrides[deps.get_job_runner] = lambda: job_runner
    yield application
    await job_runner.aclose()
    await application.state.token_verifier.aclose()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def bearer(vault: FakeKeyring, account_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {vault.token(account_id)}"}


@respx.mock
async def test_two_accounts_see_different_catalogues(client, vault, chosen):
    vault.install(respx.mock)
    alice = vault.token("alice")
    bob = vault.token("bob")
    chosen.seed(alice, "search", {"disabled_providers": ["openai"]})
    chosen.seed(bob, "search", {"disabled_providers": ["anthropic"]})

    alice_body = (
        await client.get("/v1/models", headers={"Authorization": f"Bearer {alice}"})
    ).json()
    bob_body = (await client.get("/v1/models", headers={"Authorization": f"Bearer {bob}"})).json()

    assert [m["id"] for m in alice_body["models"]] == ["anthropic:m"]
    assert [m["id"] for m in bob_body["models"]] == ["openai:m"]


@respx.mock
async def test_an_outage_refuses_the_catalogue_but_not_a_scrape_that_needs_no_model(
    client, vault, chosen
):
    vault.install(respx.mock)
    chosen.unavailable = True
    headers = bearer(vault, "alice")

    models = await client.get("/v1/models", headers=headers)
    scrape = await client.post(
        "/v1/scrape",
        headers=headers,
        json={"urls": ["https://example.com/page"], "summarize": False},
    )
    summarize = await client.post(
        "/v1/summarize", headers=headers, json={"text": "already have this"}
    )

    assert models.status_code == 503
    assert scrape.status_code == 200
    assert summarize.status_code == 503
    assert "settings-api" in models.json()["detail"]


@respx.mock
async def test_settings_api_refusing_this_service_is_a_503(client, vault, chosen):
    vault.install(respx.mock)
    chosen.rejects["search"] = (403, "web-search-api was not granted search")

    response = await client.get("/v1/models", headers=bearer(vault, "alice"))

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "granted" not in response.json()["detail"]


@respx.mock
async def test_naming_a_profile_does_not_need_the_default(client, vault, chosen):
    vault.install(respx.mock)
    chosen.unavailable = True

    response = await client.post(
        "/v1/scrape",
        headers={**bearer(vault, "alice"), "X-Keyring-Profile": "work"},
        json={"urls": ["https://example.com/page"], "summarize": False},
    )

    assert response.status_code == 200
