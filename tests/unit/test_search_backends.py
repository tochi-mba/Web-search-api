import httpx
import pytest
import respx

from app.core.errors import (
    ProviderUnavailableError,
    SearchBlockedError,
    TimeoutProblem,
    UpstreamError,
)
from app.services.keyring.caller import Caller
from app.services.keyring.client import KeyringClient
from app.services.search.base import SearchQuery, SearchResponse, SearchResult
from app.services.search.google import GoogleSearchBackend, build_search_url
from app.services.search.searxng import SearxngSearchBackend
from app.services.search.serper import SerperSearchBackend
from tests.fake_keyring import BASE_URL, FakeKeyring


class FakeBrowser:
    def __init__(self, html="", error=None):
        self.html = html
        self.error = error
        self.rendered = []

    async def render(self, url, *, wait_for_selector=None):
        self.rendered.append(url)
        if self.error:
            raise self.error
        return self.html

    async def close(self):
        return None


# --- query shaping --------------------------------------------------------- #


def test_search_url_encodes_the_query():
    url = build_search_url(SearchQuery(query="widget latency"))
    assert "q=widget+latency" in url
    assert url.startswith("https://www.google.com/search?")


def test_site_filter_is_appended():
    assert "site%3Areddit.com" in build_search_url(SearchQuery(query="best tv", site="reddit.com"))


def test_language_and_region_are_passed():
    url = build_search_url(SearchQuery(query="x", language="fr", region="ca"))
    assert "hl=fr" in url
    assert "gl=ca" in url


def test_safe_search_toggles_the_flag():
    assert "safe=active" in build_search_url(SearchQuery(query="x", safe_search=True))
    assert "safe=active" not in build_search_url(SearchQuery(query="x", safe_search=False))


def test_query_string_without_a_site_is_unchanged():
    assert SearchQuery(query="plain").to_query_string() == "plain"


# --- google backend -------------------------------------------------------- #


async def test_google_backend_returns_parsed_results(load_html):
    backend = GoogleSearchBackend(FakeBrowser(load_html("google_serp_modern.html")))
    response = await backend.search(SearchQuery(query="widget latency"))
    assert response.backend == "google"
    assert response.query == "widget latency"
    assert len(response.results) == 3


async def test_google_backend_is_always_configured():
    assert await GoogleSearchBackend(FakeBrowser()).is_configured() is True


async def test_google_backend_respects_max_results(load_html):
    backend = GoogleSearchBackend(FakeBrowser(load_html("google_serp_modern.html")))
    response = await backend.search(SearchQuery(query="x", max_results=1))
    assert len(response.results) == 1


async def test_google_backend_raises_on_a_captcha(load_html):
    backend = GoogleSearchBackend(FakeBrowser(load_html("google_captcha.html")))
    with pytest.raises(SearchBlockedError, match="captcha"):
        await backend.search(SearchQuery(query="x"))


async def test_google_backend_raises_on_a_consent_wall(load_html):
    backend = GoogleSearchBackend(FakeBrowser(load_html("google_consent.html")))
    with pytest.raises(SearchBlockedError, match="consent"):
        await backend.search(SearchQuery(query="x"))


async def test_google_backend_returns_empty_for_no_results(load_html):
    backend = GoogleSearchBackend(FakeBrowser(load_html("google_no_results.html")))
    assert (await backend.search(SearchQuery(query="zzzz"))).is_empty is True


# --- searxng backend ------------------------------------------------------- #


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


async def test_searxng_is_not_configured_without_a_base_url(client):
    assert await SearxngSearchBackend(client, base_url="").is_configured() is False


async def test_searxng_is_configured_with_a_base_url(client):
    assert await SearxngSearchBackend(client, base_url="https://s.test").is_configured() is True


@respx.mock
async def test_searxng_parses_results(client):
    respx.get("https://s.test/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "One", "url": "https://a.com/1", "content": "first"},
                    {"title": "Two", "url": "https://a.com/2", "content": "second"},
                ]
            },
        )
    )
    backend = SearxngSearchBackend(client, base_url="https://s.test/")
    response = await backend.search(SearchQuery(query="x"))
    assert [r.url for r in response.results] == ["https://a.com/1", "https://a.com/2"]
    assert response.results[0].rank == 1
    assert response.backend == "searxng"


@respx.mock
async def test_searxng_skips_entries_without_a_url(client):
    respx.get("https://s.test/search").mock(
        return_value=httpx.Response(200, json={"results": [{"title": "No URL"}]})
    )
    backend = SearxngSearchBackend(client, base_url="https://s.test")
    assert (await backend.search(SearchQuery(query="x"))).is_empty


@respx.mock
async def test_searxng_error_status_raises(client):
    respx.get("https://s.test/search").mock(return_value=httpx.Response(500))
    backend = SearxngSearchBackend(client, base_url="https://s.test")
    with pytest.raises(UpstreamError):
        await backend.search(SearchQuery(query="x"))


@respx.mock
async def test_searxng_timeout_raises(client):
    respx.get("https://s.test/search").mock(side_effect=httpx.ReadTimeout("slow"))
    backend = SearxngSearchBackend(client, base_url="https://s.test")
    with pytest.raises(TimeoutProblem):
        await backend.search(SearchQuery(query="x"))


@respx.mock
async def test_searxng_transport_error_raises(client):
    respx.get("https://s.test/search").mock(side_effect=httpx.ConnectError("down"))
    backend = SearxngSearchBackend(client, base_url="https://s.test")
    with pytest.raises(UpstreamError):
        await backend.search(SearchQuery(query="x"))


# --- serper backend -------------------------------------------------------- #


@pytest.fixture
def vault():
    return FakeKeyring()


@pytest.fixture
def serper(client, vault):
    """A Serper backend bound to a caller whose key lives in keyring."""
    keyring = KeyringClient(client, base_url=BASE_URL, service_token="svc")
    caller = Caller(account_id="acct-1", profile="personal", user_token=vault.token())
    return SerperSearchBackend(client, keyring=keyring, caller=caller)


def connected(vault, key="serper-key"):
    vault.connect("serper", headers={"X-API-KEY": key})


@respx.mock
async def test_serper_is_configured_only_once_connected(serper, vault):
    vault.install(respx.mock)
    assert await serper.is_configured() is False

    connected(vault)
    assert await serper.is_configured() is True


async def test_serper_without_a_caller_is_not_configured(client):
    assert await SerperSearchBackend(client).is_configured() is False


async def test_serper_without_a_profile_is_skipped_rather_than_guessed(client, vault):
    keyring = KeyringClient(client, base_url=BASE_URL, service_token="svc")
    caller = Caller(account_id="acct-1", profile=None, user_token=vault.token())
    backend = SerperSearchBackend(client, keyring=keyring, caller=caller)
    assert await backend.is_configured() is False


@respx.mock
async def test_for_caller_rebinds_the_credential(client, vault):
    """Two people using the same deployment use their own Serper accounts."""
    vault.install(respx.mock)
    vault.connect("serper", account_id="acct-2")

    keyring = KeyringClient(client, base_url=BASE_URL, service_token="svc")
    other = Caller(account_id="acct-2", profile="personal", user_token=vault.token("acct-2"))
    bound = SerperSearchBackend(client, keyring=keyring).for_caller(other)

    assert await bound.is_configured() is True
    assert await SerperSearchBackend(client, keyring=keyring).is_configured() is False


@respx.mock
async def test_serper_parses_organic_results(serper, vault):
    vault.install(respx.mock)
    connected(vault)
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {"title": "One", "link": "https://a.com/1", "snippet": "first"},
                    {"title": "Two", "link": "https://a.com/2", "snippet": "second"},
                ]
            },
        )
    )
    response = await serper.search(SearchQuery(query="x"))
    assert len(response.results) == 2
    assert response.results[1].rank == 2


@respx.mock
async def test_the_key_keyring_resolved_is_sent(serper, vault):
    vault.install(respx.mock)
    connected(vault, key="the-real-key")
    route = respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json={"organic": []})
    )
    await serper.search(SearchQuery(query="x"))
    assert route.calls[0].request.headers["x-api-key"] == "the-real-key"


@respx.mock
async def test_searching_without_a_connection_is_refused(serper, vault):
    vault.install(respx.mock)
    with pytest.raises(ProviderUnavailableError, match="not connected"):
        await serper.search(SearchQuery(query="x"))


@respx.mock
async def test_serper_skips_entries_without_a_link(serper, vault):
    vault.install(respx.mock)
    connected(vault)
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json={"organic": [{"title": "No link"}]})
    )
    assert (await serper.search(SearchQuery(query="x"))).is_empty


@respx.mock
async def test_serper_error_status_raises(serper, vault):
    vault.install(respx.mock)
    connected(vault)
    respx.post("https://google.serper.dev/search").mock(return_value=httpx.Response(403))
    with pytest.raises(UpstreamError):
        await serper.search(SearchQuery(query="x"))


@respx.mock
async def test_serper_timeout_raises(serper, vault):
    vault.install(respx.mock)
    connected(vault)
    respx.post("https://google.serper.dev/search").mock(side_effect=httpx.ReadTimeout("s"))
    with pytest.raises(TimeoutProblem):
        await serper.search(SearchQuery(query="x"))


@respx.mock
async def test_serper_transport_error_raises(serper, vault):
    vault.install(respx.mock)
    connected(vault)
    respx.post("https://google.serper.dev/search").mock(side_effect=httpx.ConnectError("d"))
    with pytest.raises(UpstreamError):
        await serper.search(SearchQuery(query="x"))


# --- shared dataclass behaviour -------------------------------------------- #


def test_search_response_is_empty_when_there_are_no_results():
    assert SearchResponse(query="q", backend="b").is_empty is True
    assert (
        SearchResponse(query="q", backend="b", results=[SearchResult("t", "u", "s", 1)]).is_empty
        is False
    )
