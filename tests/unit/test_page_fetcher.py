import httpx
import pytest
import respx

from app.core.errors import ForbiddenUrlError, UpstreamError
from app.services.fetch.http import HttpFetcher
from app.services.fetch.page import RENDER_FALLBACK_THRESHOLD, PageFetcher

RICH_HTML = (
    "<html><body><article>"
    + "<p>Real sentence of content here.</p>" * 40
    + "</article></body></html>"
)
SHELL_HTML = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'


def resolver(host):
    if host == "example.com":
        return ["93.184.216.34"]
    if host == "internal.test":
        return ["10.0.0.1"]
    raise OSError(host)


class FakeBrowser:
    def __init__(self, html=RICH_HTML, error=None):
        self.html = html
        self.error = error
        self.calls = []

    async def render(self, url, *, wait_for_selector=None):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.html

    async def close(self):
        return None


class AllowAllRobots:
    async def can_fetch(self, url):
        return True


class DenyAllRobots:
    async def can_fetch(self, url):
        return False


@pytest.fixture
async def http_fetcher():
    async with httpx.AsyncClient() as client:
        yield HttpFetcher(
            client,
            user_agent="test-agent",
            timeout_seconds=5.0,
            max_redirects=3,
            max_response_bytes=1_000_000,
            resolver=resolver,
        )


def build(http_fetcher, **kwargs):
    kwargs.setdefault("resolver", resolver)
    return PageFetcher(http_fetcher, **kwargs)


# --- plain HTTP ------------------------------------------------------------ #


@respx.mock
async def test_http_fetch_extracts_content(http_fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=RICH_HTML))
    page = await build(http_fetcher).fetch("https://example.com/a")
    assert "Real sentence of content" in page.content.text
    assert page.rendered is False
    assert page.final_url == "https://example.com/a"


@respx.mock
async def test_never_mode_skips_the_browser(http_fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=SHELL_HTML))
    browser = FakeBrowser()
    page = await build(http_fetcher, browser=browser).fetch("https://example.com/a", render="never")
    assert browser.calls == []
    assert page.rendered is False


# --- rendering ------------------------------------------------------------- #


@respx.mock
async def test_always_mode_uses_the_browser_without_an_http_call(http_fetcher):
    route = respx.get("https://example.com/a").mock(
        return_value=httpx.Response(200, html=RICH_HTML)
    )
    browser = FakeBrowser()
    page = await build(http_fetcher, browser=browser).fetch(
        "https://example.com/a", render="always"
    )
    assert page.rendered is True
    assert browser.calls == ["https://example.com/a"]
    assert route.call_count == 0


@respx.mock
async def test_auto_mode_escalates_when_http_returns_a_shell(http_fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=SHELL_HTML))
    browser = FakeBrowser(RICH_HTML)
    page = await build(http_fetcher, browser=browser).fetch("https://example.com/a")
    assert page.rendered is True
    assert browser.calls == ["https://example.com/a"]


@respx.mock
async def test_auto_mode_does_not_escalate_when_http_is_sufficient(http_fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=RICH_HTML))
    browser = FakeBrowser()
    page = await build(http_fetcher, browser=browser).fetch("https://example.com/a")
    assert page.rendered is False
    assert browser.calls == []
    assert len(page.content.text) >= RENDER_FALLBACK_THRESHOLD


@respx.mock
async def test_auto_mode_keeps_http_content_when_rendering_does_not_help(http_fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=SHELL_HTML))
    browser = FakeBrowser(SHELL_HTML)
    page = await build(http_fetcher, browser=browser).fetch("https://example.com/a")
    assert page.rendered is False


@respx.mock
async def test_auto_mode_degrades_gracefully_when_the_browser_fails(http_fetcher):
    """A browser problem must not lose a page we already fetched over HTTP."""
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=SHELL_HTML))
    browser = FakeBrowser(error=UpstreamError("browser died"))
    page = await build(http_fetcher, browser=browser).fetch("https://example.com/a")
    assert page.rendered is False


@respx.mock
async def test_auto_mode_without_a_browser_returns_the_thin_content(http_fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=SHELL_HTML))
    page = await build(http_fetcher).fetch("https://example.com/a")
    assert page.rendered is False


async def test_always_mode_without_a_browser_is_an_error(http_fetcher):
    with pytest.raises(UpstreamError, match="Rendering unavailable"):
        await build(http_fetcher).fetch("https://example.com/a", render="always")


# --- policy ---------------------------------------------------------------- #


async def test_private_urls_are_rejected(http_fetcher):
    with pytest.raises(ForbiddenUrlError):
        await build(http_fetcher).fetch("https://internal.test/admin")


@respx.mock
async def test_robots_denial_blocks_the_fetch(http_fetcher):
    route = respx.get("https://example.com/a").mock(
        return_value=httpx.Response(200, html=RICH_HTML)
    )
    fetcher = build(http_fetcher, robots=DenyAllRobots(), respect_robots=True)
    with pytest.raises(ForbiddenUrlError, match=r"robots\.txt"):
        await fetcher.fetch("https://example.com/a")
    assert route.call_count == 0


@respx.mock
async def test_robots_allowance_permits_the_fetch(http_fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=RICH_HTML))
    fetcher = build(http_fetcher, robots=AllowAllRobots(), respect_robots=True)
    assert (await fetcher.fetch("https://example.com/a")).content.text


@respx.mock
async def test_robots_can_be_ignored_by_configuration(http_fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=RICH_HTML))
    fetcher = build(http_fetcher, robots=DenyAllRobots(), respect_robots=False)
    assert (await fetcher.fetch("https://example.com/a")).content.text


@respx.mock
async def test_no_robots_policy_means_no_check(http_fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=RICH_HTML))
    fetcher = build(http_fetcher, robots=None, respect_robots=True)
    assert (await fetcher.fetch("https://example.com/a")).content.text


@respx.mock
async def test_render_failure_from_a_blocked_url_degrades(http_fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, html=SHELL_HTML))
    browser = FakeBrowser(error=ForbiddenUrlError("blocked"))
    page = await build(http_fetcher, browser=browser).fetch("https://example.com/a")
    assert page.rendered is False
