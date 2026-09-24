"""Whether a browser is really there, which readiness used to answer from a config flag.

`app.state.browser_available` was `resolve_executable_path() is not None or
settings.browser_headless`, and `browser_headless` defaults to true — so `/ready` reported
"headless chromium ready" on a machine where every launch failed with

    Executable doesn't exist at /ms-playwright/chromium_headless_shell-1234/...

It stayed that way until somebody asked the assistant a question and it had to say the web was
unreachable. A readiness check that cannot fail is not a readiness check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.services.fetch import browser as browser_module
from app.services.fetch.browser import browser_is_launchable


async def test_an_explicit_binary_that_exists_is_enough(tmp_path: Path) -> None:
    binary = tmp_path / "chromium"
    binary.write_text("", encoding="utf-8")
    ready, detail = await browser_is_launchable(str(binary))
    assert ready is True
    assert str(binary) in detail


async def test_playwright_reporting_a_browser_that_is_there_is_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    present = tmp_path / "headless_shell"
    present.write_text("", encoding="utf-8")
    _pretend_playwright_reports(monkeypatch, str(present))
    ready, detail = await browser_is_launchable()
    assert ready is True
    assert str(present) in detail


async def test_playwright_reporting_a_browser_that_is_not_there_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real failure: the image carried browsers for one Playwright and the library was
    looking for another's."""
    missing = tmp_path / "chromium_headless_shell-1234" / "chrome-headless-shell"
    _pretend_playwright_reports(monkeypatch, str(missing))
    ready, detail = await browser_is_launchable()
    assert ready is False
    assert "which is not there" in detail
    assert str(missing) in detail


async def test_playwright_refusing_to_answer_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """No driver, no browsers installed, a broken install — all the same answer."""

    class _Starter:
        async def start(self) -> Any:
            msg = "no driver"
            raise RuntimeError(msg)

    _install_fake_playwright(monkeypatch, _Starter)
    ready, detail = await browser_is_launchable()
    assert ready is False
    assert "RuntimeError" in detail


def _pretend_playwright_reports(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    class _Chromium:
        executable_path = path

    class _Driver:
        chromium = _Chromium()

        async def stop(self) -> None:
            return None

    class _Starter:
        async def start(self) -> _Driver:
            return _Driver()

    _install_fake_playwright(monkeypatch, _Starter)


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    import sys
    import types

    monkeypatch.setattr(browser_module, "resolve_executable_path", lambda _explicit=None: None)
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
