"""Headless browser access, behind a protocol so tests need not launch one.

``PlaywrightBrowserSession`` owns a single browser process and hands out a fresh
context per navigation, which keeps cookies and storage from leaking between
unrelated requests.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.core.errors import TimeoutProblem, UpstreamError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright

logger = get_logger(__name__)

#: Resource types blocked while rendering: none of them affect extracted text.
_BLOCKED_RESOURCES = frozenset({"image", "media", "font", "stylesheet"})

#: Locations checked for a system Chromium when Playwright's own download is
#: absent or is a different build than the installed Playwright expects. Common
#: in containers that ship a browser separately from the Python package.
_SYSTEM_CHROMIUM_PATHS = (
    "/opt/pw-browsers/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


def resolve_executable_path(explicit: str | None = None) -> str | None:
    """Find a Chromium binary to launch, or ``None`` to let Playwright choose.

    Checked in order: an explicit path, ``WSA_BROWSER_EXECUTABLE_PATH``, then
    the well-known system locations. Returning ``None`` means "use the browser
    Playwright downloaded for itself", which is the normal case on a developer
    machine.
    """
    candidates = (explicit, os.environ.get("WSA_BROWSER_EXECUTABLE_PATH"), *_SYSTEM_CHROMIUM_PATHS)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


@runtime_checkable
class RouteLike(Protocol):
    """The part of Playwright's ``Route`` the resource filter uses."""

    async def abort(self) -> None:
        """Cancel the request."""
        ...

    async def continue_(self) -> None:
        """Let the request proceed."""
        ...


@runtime_checkable
class RequestLike(Protocol):
    """The part of Playwright's ``Request`` the resource filter uses."""

    @property
    def resource_type(self) -> str:
        """What kind of resource is being requested."""
        ...


@runtime_checkable
class BrowserSession(Protocol):
    """Anything that can render a URL and return its HTML."""

    async def render(self, url: str, *, wait_for_selector: str | None = None) -> str:
        """Navigate to ``url`` and return the resulting HTML."""
        ...

    async def close(self) -> None:
        """Release any underlying resources."""
        ...


class PlaywrightBrowserSession:
    """A real headless Chromium, started lazily and reused across requests."""

    def __init__(
        self,
        *,
        headless: bool = True,
        navigation_timeout_ms: int = 20_000,
        user_agent: str | None = None,
        locale: str = "en-US",
        block_heavy_resources: bool = True,
        executable_path: str | None = None,
    ) -> None:
        """Configure the session without starting anything yet."""
        self._headless = headless
        self._timeout = navigation_timeout_ms
        self._user_agent = user_agent
        self._locale = locale
        self._block_heavy = block_heavy_resources
        self._executable_path = resolve_executable_path(executable_path)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    @property
    def started(self) -> bool:
        """Whether the underlying browser process is running."""
        return self._browser is not None

    async def start(self) -> None:
        """Launch the browser process if it is not already running."""
        if self._browser is not None:
            return

        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                executable_path=self._executable_path,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
        except Exception as exc:
            raise UpstreamError(
                "Browser unavailable", detail=f"Could not launch headless Chromium: {exc}"
            ) from exc

        logger.info(
            "browser.started",
            headless=self._headless,
            executable_path=self._executable_path,
        )

    async def render(self, url: str, *, wait_for_selector: str | None = None) -> str:
        """Navigate to ``url`` in a fresh context and return the page HTML.

        Args:
            url: Where to navigate.
            wait_for_selector: Optional selector to wait for before reading the
                DOM. A timeout waiting for it is not fatal - the page is read
                as-is, because a missing selector often means a block page.

        Returns:
            The page's HTML after navigation settled.
        """
        await self.start()
        assert self._browser is not None  # noqa: S101 - guaranteed by start()

        context = await self._browser.new_context(
            user_agent=self._user_agent,
            locale=self._locale,
            viewport={"width": 1280, "height": 900},
        )
        try:
            if self._block_heavy:
                await context.route("**/*", _block_heavy_resources)

            page = await context.new_page()
            page.set_default_timeout(self._timeout)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout)
            except Exception as exc:
                raise TimeoutProblem(
                    "Navigation failed", detail=f"Could not load {url}: {exc}"
                ) from exc

            if wait_for_selector:
                try:
                    await page.wait_for_selector(wait_for_selector, timeout=self._timeout)
                except Exception:
                    logger.debug("browser.selector_absent", url=url, selector=wait_for_selector)

            html: str = await page.content()
            return html
        finally:
            await context.close()

    async def close(self) -> None:
        """Shut the browser process down."""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("browser.stopped")

    async def __aenter__(self) -> PlaywrightBrowserSession:
        """Start the browser on context entry."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the browser on context exit."""
        await self.close()


async def _block_heavy_resources(route: RouteLike, request: RequestLike) -> None:
    """Abort requests for resources that never affect extracted text."""
    if request.resource_type in _BLOCKED_RESOURCES:
        await route.abort()
    else:
        await route.continue_()
