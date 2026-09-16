# 0003 — Playwright stays behind one adapter, enforced in CI

**Status:** Accepted

## Context

Google has no free results API worth using, so results come from a rendered search page.
Some ordinary pages are client-rendered and return a near-empty shell to a plain GET. Both
facts mean a real browser.

A browser is by far the heaviest dependency here. It is a separate process and hundreds of
megabytes of memory; its Python package and the Chromium actually installed can disagree,
which they routinely do in containers; and it is the one dependency that makes a test suite
slow and flaky if it spreads.

The failure mode to avoid is Playwright leaking upward — a `page.goto` in the search
backend, a `browser.new_context` in the scrape route — until the browser is impossible to
replace and impossible to test without.

## Decision

**Playwright is imported in exactly one module.** `app/services/fetch/browser.py` defines a
`BrowserSession` protocol — `render(url, wait_for_selector=None) -> str` and `close()` —
and `PlaywrightBrowserSession` is its only implementation. The Google backend and the page
fetcher take the protocol and have never heard of Playwright.

**The boundary is enforced, not documented.** An import-linter contract in `pyproject.toml`,
"Playwright stays behind the browser adapter", forbids the `playwright` package to every
other module, and `make imports` runs it as part of `make check`. The contract names
`app.services.fetch.page` explicitly, which is the interesting case: the page fetcher
constructs the adapter, so it must import that module, and must still not reach Playwright
itself.

Details that follow from the decision:

- **`async_playwright` is imported inside `start()`**, not at module scope. Importing the
  adapter costs nothing, and the dependency is only paid when a browser is actually
  launched. The type-only imports sit under `TYPE_CHECKING`.
- **One browser process, a fresh context per navigation.** Cookies and storage never leak
  between unrelated requests, which matters when the requests belong to different people.
- **Failures are translated at the boundary.** A launch failure becomes `UpstreamError`, a
  navigation failure `TimeoutProblem`. Nothing above the adapter catches a Playwright
  exception, because nothing above it can name one.
- **A failed launch closes the driver it already started.** Launching can fail after the
  driver process is up, and that driver still owns pipes and a subprocess with no browser
  to use.
- **Waiting for a selector is best-effort.** A timeout waiting for one is not fatal and the
  page is read as-is, because a missing results selector usually means a block page — and
  reading it is exactly how the SERP parser detects a consent wall or a CAPTCHA.

## Consequences

The port is narrow on purpose: HTML in, HTML out. That covers both things this service
needs and nothing else. Anything wanting to click, type or capture a download does not fit
and would have to widen the protocol rather than reach past it.

**Tests stay fast.** Browser tests run real headless Chromium against a local fixture
server, never against Google, so they cover navigation, resource blocking and context
lifecycle for real without depending on anybody's current markup. Every other test in the
suite injects a stub satisfying the protocol and launches nothing.

Heavy resources — images, media, fonts, stylesheets — are aborted during rendering, because
none of them affects extracted text.

`resolve_executable_path()` exists entirely because of the version-mismatch problem:
`WSA_BROWSER_EXECUTABLE_PATH` and then a list of well-known system locations are checked
before falling back to the browser Playwright downloaded for itself. That is container
reality intruding, and it is confined to this module too.

The enforcement is the load-bearing part. A documented layering rule decays quietly; a
failing `make check` does not.

## What would change this

A rendering backend that is not a local browser — a remote browser service, or a rendering
API — would slot in behind `BrowserSession` without touching the search backend or the page
fetcher. That is precisely why the protocol exists, and taking it would be a bootstrap
change and nothing more.

Needing interaction rather than rendering — logging into a site, capturing a download —
would change the port itself, and should be a deliberate widening with its own record
rather than a second import of Playwright somewhere else.
