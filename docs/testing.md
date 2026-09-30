# Testing

Coverage is enforced at **100%** (`fail_under = 100`). That is only sustainable
because the design keeps I/O at the edges and injects collaborators.

```bash
make check          # format and lint, strict types, import contracts, tests at 100%
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

### The Anthropic SDK

The `anthropic` SDK is built on **`httpx2`**, which `respx` cannot patch. Two
options: stub the SDK methods, or run a real server.

We run a real server (`tests/mock_api_server.py`): a recording
`ThreadingHTTPServer` that serves queued responses and captures the exact
request. The SDK's `base_url` points at it.

This matters because the entire reason that adapter exists is request shaping.
A test that stubs `messages.create()` and asserts on the kwargs proves the test
harness works. A test that reads the JSON body off a socket proves Opus 5 got
`thinking: {"type": "adaptive"}` and Haiku 4.5 got `budget_tokens`.

### Playwright

Real headless Chromium, launched against a **local static server** serving the
same HTML fixtures the parser unit tests use. That covers navigation, resource
blocking and context lifecycle for real, with no network flakiness and no
dependence on Google's current markup.

Marked `@pytest.mark.browser` and included in the default run, so `make check`
needs a Chromium. `resolve_executable_path()` looks for one in this order:
`WSA_BROWSER_EXECUTABLE_PATH`, then `/opt/pw-browsers/chromium`, `/usr/bin/chromium`,
`/usr/bin/chromium-browser` and `/usr/bin/google-chrome`, then the build Playwright
downloaded for itself. On a machine with none of those, run
`uv run playwright install chromium` once. Where a system Chromium already exists
at one of those paths, it is used and nothing needs installing. CI also runs the
browser tests on their own, with `pytest -m browser`, after installing Chromium.

### The provider fleet

Parametrised tests in `tests/unit/test_openai_compatible.py` run **every** row of
`OPENAI_COMPATIBLE_SPECS` against `respx` routes standing in for a compliant server,
asserting each can list models and chat, and that each spec is well formed. Adding a
provider is covered automatically; a malformed row fails CI. This is what makes a 100%
gate tractable across 52 vendors.

### Endpoints

`httpx.ASGITransport` against the real app, with fakes injected through
`app.dependency_overrides`. Tests cover the happy path, partial batch failure,
per-item error codes, validation rejections and problem+json rendering.

## Fixtures

`tests/fixtures/html/` holds saved HTML: an article with metadata and
boilerplate, a minimal page, an empty page, and five Google SERP variants —
modern layout, legacy layout, consent wall, CAPTCHA interstitial, no results.

These fixtures are the honest record of the markup the parser expects. When
Google changes, update the fixture and the parser in the same commit.

## What is deliberately not tested here

Live LLM calls and live Google scraping cost money and depend on third parties.
They belong in a manual run against a real server:

```bash
make run                                                   # in one shell
uv run python scripts/smoke.py --model ollama:llama3.1:8b  # in another
```

`scripts/smoke.py` checks health and readiness, lists models, then summarises, scrapes
and searches once each. It exits 0 only if every call answered 200 and no batch item came
back in error, and it lists what failed otherwise. A provider key in the environment does
nothing, because this service never reads one. Without a user token it can only use a
credential-free runtime such as a local Ollama. To try a credentialed provider, set up
keyring as in [keyring.md](keyring.md) and pass the token with `--token` or
`SMOKE_USER_TOKEN` (and the gate's key with `--api-key` if `WSA_API_KEYS` is set):

```bash
SMOKE_USER_TOKEN=$USER_TOKEN uv run python scripts/smoke.py --model anthropic:claude-opus-5
```

Or make the calls yourself:

```bash
curl -s localhost:8006/v1/models -H "Authorization: Bearer $USER_TOKEN" \
  | jq '.default_model, .providers'
curl -s -X POST localhost:8006/v1/scrape -H "Authorization: Bearer $USER_TOKEN" \
  -H 'content-type: application/json' -d '{"urls":["https://example.com"]}' | jq
```
