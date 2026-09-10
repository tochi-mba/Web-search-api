import httpx
import pytest
import respx

from app.core.errors import ForbiddenUrlError, TimeoutProblem, UpstreamError
from app.services.fetch.http import HttpFetcher

PUBLIC = {
    "example.com": ["93.184.216.34"],
    "other.com": ["93.184.216.35"],
    "evil.com": ["93.184.216.36"],
}


def resolver(host):
    if host in PUBLIC:
        return PUBLIC[host]
    if host == "internal.test":
        return ["10.0.0.1"]
    raise OSError(host)


@pytest.fixture
async def fetcher():
    async with httpx.AsyncClient() as client:
        yield HttpFetcher(
            client,
            user_agent="test-agent",
            timeout_seconds=5.0,
            max_redirects=3,
            max_response_bytes=1000,
            resolver=resolver,
        )


@respx.mock
async def test_successful_fetch_returns_the_body(fetcher):
    respx.get("https://example.com/page").mock(return_value=httpx.Response(200, html="<h1>hi</h1>"))
    result = await fetcher.fetch("https://example.com/page")
    assert result.status_code == 200
    assert "<h1>hi</h1>" in result.body
    assert result.content_type == "text/html"
    assert result.redirected is False


@respx.mock
async def test_sends_the_configured_user_agent(fetcher):
    route = respx.get("https://example.com/page").mock(return_value=httpx.Response(200, html="ok"))
    await fetcher.fetch("https://example.com/page")
    assert route.calls[0].request.headers["user-agent"] == "test-agent"


@respx.mock
async def test_follows_a_safe_redirect(fetcher):
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"location": "https://other.com/b"})
    )
    respx.get("https://other.com/b").mock(return_value=httpx.Response(200, html="done"))
    result = await fetcher.fetch("https://example.com/a")
    assert result.final_url == "https://other.com/b"
    assert result.url == "https://example.com/a"
    assert result.redirected is True


@respx.mock
async def test_relative_redirects_are_resolved(fetcher):
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"location": "/b"})
    )
    respx.get("https://example.com/b").mock(return_value=httpx.Response(200, html="done"))
    assert (await fetcher.fetch("https://example.com/a")).final_url == "https://example.com/b"


@respx.mock
async def test_redirect_to_a_private_address_is_blocked(fetcher):
    respx.get("https://evil.com/a").mock(
        return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/creds"})
    )
    with pytest.raises(ForbiddenUrlError):
        await fetcher.fetch("https://evil.com/a")


@respx.mock
async def test_redirect_to_a_private_hostname_is_blocked(fetcher):
    respx.get("https://evil.com/a").mock(
        return_value=httpx.Response(302, headers={"location": "https://internal.test/x"})
    )
    with pytest.raises(ForbiddenUrlError):
        await fetcher.fetch("https://evil.com/a")


@respx.mock
async def test_redirect_without_a_location_is_an_error(fetcher):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(302))
    with pytest.raises(UpstreamError, match="Malformed redirect"):
        await fetcher.fetch("https://example.com/a")


@respx.mock
async def test_redirect_loops_are_bounded(fetcher):
    respx.get("https://example.com/loop").mock(
        return_value=httpx.Response(302, headers={"location": "https://example.com/loop"})
    )
    with pytest.raises(ForbiddenUrlError, match="Too many redirects"):
        await fetcher.fetch("https://example.com/loop")


@respx.mock
async def test_error_status_is_reported(fetcher):
    respx.get("https://example.com/gone").mock(return_value=httpx.Response(404))
    with pytest.raises(UpstreamError, match="404"):
        await fetcher.fetch("https://example.com/gone")


@respx.mock
async def test_binary_content_types_are_rejected(fetcher):
    respx.get("https://example.com/file.pdf").mock(
        return_value=httpx.Response(
            200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}
        )
    )
    with pytest.raises(UpstreamError, match="Unsupported content type"):
        await fetcher.fetch("https://example.com/file.pdf")


@respx.mock
async def test_missing_content_type_is_accepted(fetcher):
    respx.get("https://example.com/x").mock(
        return_value=httpx.Response(200, content=b"plain bytes", headers={})
    )
    assert (await fetcher.fetch("https://example.com/x")).status_code == 200


@respx.mock
async def test_oversized_bodies_are_truncated(fetcher):
    respx.get("https://example.com/big").mock(return_value=httpx.Response(200, html="x" * 5000))
    assert len((await fetcher.fetch("https://example.com/big")).body) <= 1000


@respx.mock
async def test_timeouts_surface_as_timeout_problems(fetcher):
    respx.get("https://example.com/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TimeoutProblem):
        await fetcher.fetch("https://example.com/slow")


@respx.mock
async def test_connection_errors_surface_as_upstream_errors(fetcher):
    respx.get("https://example.com/down").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(UpstreamError, match="Fetch failed"):
        await fetcher.fetch("https://example.com/down")


async def test_private_url_is_rejected_before_any_request(fetcher):
    with pytest.raises(ForbiddenUrlError):
        await fetcher.fetch("https://internal.test/admin")
