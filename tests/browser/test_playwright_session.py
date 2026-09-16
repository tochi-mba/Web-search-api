"""Exercise the real Playwright code path against local fixtures.

These tests launch an actual headless Chromium. They deliberately never touch
Google or any other public site: the fixture server serves the same saved HTML
the parser unit tests use, so this covers navigation, resource blocking and
context lifecycle without any network flakiness.
"""

from __future__ import annotations

import pytest

from app.core.errors import TimeoutProblem
from app.services.fetch.browser import PlaywrightBrowserSession
from app.services.search.serp_parser import parse_google_serp

pytestmark = pytest.mark.browser


@pytest.fixture
async def session():
    browser = PlaywrightBrowserSession(headless=True, navigation_timeout_ms=15_000)
    try:
        yield browser
    finally:
        await browser.close()


async def test_renders_a_page(session, fixture_server):
    html = await session.render(f"{fixture_server}/article.html")
    assert "Understanding Widget Latency" in html


async def test_rendered_serp_feeds_the_parser(session, fixture_server):
    html = await session.render(f"{fixture_server}/google_serp_modern.html")
    results = parse_google_serp(html)
    assert len(results) == 3
    assert results[0].url == "https://example.com/latency-guide"


async def test_starts_lazily_and_reports_state(session, fixture_server):
    assert session.started is False
    await session.render(f"{fixture_server}/minimal.html")
    assert session.started is True


async def test_browser_is_reused_across_renders(session, fixture_server):
    await session.render(f"{fixture_server}/minimal.html")
    first = session._browser
    await session.render(f"{fixture_server}/article.html")
    assert session._browser is first


async def test_wait_for_selector_succeeds_when_present(session, fixture_server):
    html = await session.render(f"{fixture_server}/article.html", wait_for_selector="article")
    assert "dispatch layer" in html


async def test_missing_selector_is_not_fatal(session, fixture_server):
    html = await session.render(
        f"{fixture_server}/minimal.html", wait_for_selector="#definitely-not-here"
    )
    assert "one short paragraph" in html


async def test_navigation_failure_raises_timeout_problem(session):
    with pytest.raises(TimeoutProblem, match="Navigation failed"):
        await session.render("http://127.0.0.1:1/nothing")


async def test_close_is_idempotent(session, fixture_server):
    await session.render(f"{fixture_server}/minimal.html")
    await session.close()
    await session.close()
    assert session.started is False


async def test_context_manager_starts_and_stops():
    async with PlaywrightBrowserSession(headless=True) as browser:
        assert browser.started is True
    assert browser.started is False


async def test_heavy_resources_are_blocked_during_render(session, fixture_server):
    """The route handler must abort images and allow documents."""
    html = await session.render(f"{fixture_server}/article.html")
    assert "Understanding Widget Latency" in html


async def test_resource_blocking_can_be_disabled(fixture_server):
    browser = PlaywrightBrowserSession(headless=True, block_heavy_resources=False)
    try:
        html = await browser.render(f"{fixture_server}/article.html")
        assert "dispatch layer" in html
    finally:
        await browser.close()


async def test_launch_failure_is_reported_as_upstream_error():
    from app.core.errors import UpstreamError

    browser = PlaywrightBrowserSession(headless=True, executable_path=None)
    browser._executable_path = "/nonexistent/chromium-binary"
    with pytest.raises(UpstreamError, match="Browser unavailable"):
        await browser.start()
    try:
        assert browser._playwright is None
        assert browser.started is False
    finally:
        await browser.close()
