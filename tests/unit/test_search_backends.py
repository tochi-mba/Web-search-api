import httpx
import pytest
import respx

from app.core.errors import SearchBlockedError, TimeoutProblem, UpstreamError
from app.services.search.base import SearchQuery, SearchResponse, SearchResult
from app.services.search.google import GoogleSearchBackend, build_search_url
from app.services.search.searxng import SearxngSearchBackend
from app.services.search.serper import SerperSearchBackend


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


async def test_serper_requires_an_api_key(client):
    assert await SerperSearchBackend(client, api_key="").is_configured() is False
    assert await SerperSearchBackend(client, api_key="k").is_configured() is True


@respx.mock
async def test_serper_parses_organic_results(client):
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
    backend = SerperSearchBackend(client, api_key="k")
    response = await backend.search(SearchQuery(query="x"))
    assert len(response.results) == 2
    assert response.results[1].rank == 2


@respx.mock
async def test_serper_sends_the_api_key(client):
    route = respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json={"organic": []})
    )
    await SerperSearchBackend(client, api_key="secret").search(SearchQuery(query="x"))
    assert route.calls[0].request.headers["x-api-key"] == "secret"


@respx.mock
async def test_serper_skips_entries_without_a_link(client):
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json={"organic": [{"title": "No link"}]})
    )
    assert (await SerperSearchBackend(client, api_key="k").search(SearchQuery(query="x"))).is_empty


@respx.mock
async def test_serper_error_status_raises(client):
    respx.post("https://google.serper.dev/search").mock(return_value=httpx.Response(403))
    with pytest.raises(UpstreamError):
        await SerperSearchBackend(client, api_key="k").search(SearchQuery(query="x"))


@respx.mock
async def test_serper_timeout_raises(client):
    respx.post("https://google.serper.dev/search").mock(side_effect=httpx.ReadTimeout("s"))
    with pytest.raises(TimeoutProblem):
        await SerperSearchBackend(client, api_key="k").search(SearchQuery(query="x"))


@respx.mock
async def test_serper_transport_error_raises(client):
    respx.post("https://google.serper.dev/search").mock(side_effect=httpx.ConnectError("d"))
    with pytest.raises(UpstreamError):
        await SerperSearchBackend(client, api_key="k").search(SearchQuery(query="x"))


# --- shared dataclass behaviour -------------------------------------------- #


def test_search_response_is_empty_when_there_are_no_results():
    assert SearchResponse(query="q", backend="b").is_empty is True
    assert (
        SearchResponse(query="q", backend="b", results=[SearchResult("t", "u", "s", 1)]).is_empty
        is False
    )
