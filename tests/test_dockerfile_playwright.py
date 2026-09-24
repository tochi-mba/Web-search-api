"""The image's browsers and the locked Playwright have to be the same release.

`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` in the Dockerfile means the base image's browsers are
the only ones there are, and every Playwright release looks for its own build number. When the
lock moved to 1.62 against a 1.48 image, every search failed with

    Executable doesn't exist at /ms-playwright/chromium_headless_shell-1234/...

and nothing caught it: the container built, started, passed its health check, and answered
`/ready` with "headless chromium ready". It surfaced when a person asked a question and the
assistant had to say the web was unreachable.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def locked_playwright() -> str:
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    found = re.search(r'name = "playwright"\nversion = "([^"]+)"', lock)
    assert found, "playwright is not in uv.lock"
    return found.group(1)


def base_image_tag() -> str:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    found = re.search(r"^FROM mcr\.microsoft\.com/playwright/python:v([\d.]+)-", dockerfile, re.M)
    assert found, "the Dockerfile does not build on a pinned Playwright image"
    return found.group(1)


def test_the_image_carries_the_browsers_the_locked_playwright_looks_for() -> None:
    assert base_image_tag() == locked_playwright(), (
        "the Playwright image tag and the locked playwright version have drifted; "
        "the image's browsers will not be the ones the library goes looking for"
    )
