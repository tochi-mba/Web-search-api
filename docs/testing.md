# Testing

Coverage is enforced at **100%** (`fail_under = 100`). That is only sustainable
because the design keeps I/O at the edges and injects collaborators.

```bash
make check          # format, lint, types, tests with the coverage gate
make test           # tests only
uv run pytest -m browser              # real Chromium tests
uv run pytest --cov=app --cov-report=term-missing
```

## Principles

**No test touches the real internet.** Not Google, not any LLM API. A suite that
depends on a third party is a suite that fails on Friday afternoon for reasons
unrelated to the change.

**Mock at the lowest honest layer.** Stubbing the method under test proves
nothing. Where possible we assert the bytes that would actually go on the wire.

**One test, one claim.** Test names read as sentences describing behaviour, not
as method names.

## How each layer is tested

### Pure logic

Cleaner, truncator, extractor, SERP parser and the capability layer are pure
functions. They are tested directly, with `hypothesis` for the invariants that
matter — truncation never exceeds its limit, cleaning is idempotent.

### HTTP clients

`respx` intercepts `httpx` at the transport layer. Covers success, 401, 429,
500, timeout, connection failure and malformed JSON for every client.

### The Anthropic and OpenAI SDKs

These SDKs are built on **`httpx2`**, which `respx` cannot patch. Two options:
stub the SDK methods, or run a real server.

We run a real server (`tests/mock_api_server.py`): a recording
`ThreadingHTTPServer` that serves queued responses and captures the exact
request. The SDK's `base_url` points at it.

This matters because the entire reason those adapters exist is request shaping.
A test that stubs `messages.create()` and asserts on the kwargs proves the test
harness works. A test that reads the JSON body off a socket proves Opus 5 got
`thinking: {"type": "adaptive"}` and Haiku 4.5 got `budget_tokens`.

### Playwright

Real headless Chromium, launched against a **local static server** serving the
same HTML fixtures the parser unit tests use. That covers navigation, resource
blocking and context lifecycle for real, with no network flakiness and no
dependence on Google's current markup.

Marked `@pytest.mark.browser` and included in the default run.

> In this container, Playwright's bundled Chromium build does not match the
> installed browser. `resolve_executable_path()` discovers the system Chromium
> at `/opt/pw-browsers/chromium`. Do not run `playwright install`.

### The provider fleet

One parametrised test runs **every** row of `PROVIDER_SPECS` against a mock
OpenAI-compatible server, asserting each can probe, list models and chat, and
that each spec is well formed. Adding a provider is covered automatically;
a malformed row fails CI. This is what makes a 100% gate tractable across ~50
vendors.

### Endpoints

`httpx.ASGITransport` against the real app, with fakes injected through
`app.dependency_overrides`. Tests cover the happy path, partial batch failure,
per-item error codes, validation rejections and problem+json rendering.

## Fixtures

`tests/fixtures/html/` holds saved HTML: an article with metadata and
boilerplate, a minimal page, an empty page, and four Google SERP variants —
modern layout, legacy layout, consent wall, CAPTCHA interstitial, no results.

These fixtures are the honest record of the markup the parser expects. When
Google changes, update the fixture and the parser in the same commit.

## What is deliberately not tested here

Live LLM calls and live Google scraping cost money and depend on third parties.
They belong in a manual smoke run with real keys:

```bash
export ANTHROPIC_API_KEY=...
make run
curl -s localhost:8006/v1/models | jq '.default_model, .providers'
curl -s -X POST localhost:8006/v1/scrape -H 'content-type: application/json' \
  -d '{"urls":["https://example.com"]}' | jq
```
