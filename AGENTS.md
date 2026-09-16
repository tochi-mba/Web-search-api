# AGENTS.md

Conventions and context for anyone — human or agent — working in this repository.

## What this is

A FastAPI service that scrapes Google search results and web pages and
synthesises executive summaries using any of ~60 LLM providers. API only, no UI,
intended to sit behind an MCP server.

## Non-negotiables

1. **Tests come first.** This codebase was built test-first and the coverage
   gate is `fail_under = 100`. A change without tests will fail CI.
2. **`make check` must pass** before any commit: `lint` (ruff format + check),
   `type` (mypy strict), `imports` (import-linter contracts), and `test`
   (pytest at 100% branch coverage).
3. **Never weaken the coverage gate** to get a change through. If a line is
   genuinely unreachable, restructure it rather than adding a `pragma`.
4. **Never let a credential become process-wide again.** Keys belong to callers,
   arrive per call as a `ResolvedAuth`, and are never held on a provider
   instance, cached globally, or read from the environment. The catalogue cache
   is keyed on the *verified* account id — never on anything a caller supplies.
5. **Never disable the SSRF guard.** See `docs/security.md`. `WSA_ALLOW_PRIVATE_NETWORKS`
   exists for trusted internal deployments; cloud metadata hosts stay blocked
   even then.
6. **No real network calls in tests.** Not Google, not any LLM API. See below.

## Layout

```
app/
  main.py         FastAPI factory, exception handlers, lifespan
  bootstrap.py    Composition root — builds every long-lived service
  config.py       pydantic-settings, WSA_ prefix
  constants.py    Truncation thresholds and fixed defaults
  core/           logging, middleware, errors, concurrency, cache
  schemas/        pydantic request/response models (API boundary)
  services/       the actual work
    urlsafety.py  SSRF guard
    robots.py     robots.txt policy
    text/         cleaner, truncator, extractor
    fetch/        http, browser, page orchestration
    search/       backend protocol, Google, SearxNG, Serper, failover router
    llm/          base protocol, specs table, capabilities, providers, registry,
                  prompts, summarizer
    jobs/         job types, in-memory store, background runner
    keyring/      credential resolution, local token verification, Caller
    preferences.py per-person settings from settings-api (ships dark)
    pipelines.py  the work behind each endpoint, shared by sync and background
  api/            deps (DI), mapping, routes
tests/
  unit/           one module per source module
  integration/    endpoints via ASGITransport with fakes injected
  browser/        real Chromium against a local fixture server
  fixtures/html/  saved HTML — the honest record of markup we expect
```

## Conventions

- **Python 3.11**, fully typed, `mypy --strict` on `app/`. Tests are type-checked
  but not required to annotate every fixture. Import-linter contracts in
  `pyproject.toml` keep `keyring_client` in `app.services.keyring` and
  `app.config`, `settings_client` in `app.services.preferences`, Playwright
  in the browser adapter, and the API layer out of services.
- **Google-style docstrings** on every public module, class and function.
- **Line length 100.** `ruff format` decides formatting; do not hand-wrap.
- **Comments explain *why*, never *what*.** If a comment restates the code,
  delete it. Comments earn their place by recording a constraint, a trade-off,
  or a non-obvious failure mode.
- **Errors are `DomainError` subclasses** (`app/core/errors.py`). Routes never
  deal in HTTP status codes; the handler renders RFC 9457 problem+json. New
  failure modes get a subclass, and `code` is derived from the class name.
- **Dependencies are injected via `app/api/deps.py`.** Tests override with
  `app.dependency_overrides`; never monkeypatch internals from an endpoint test.
- **Batch endpoints never fail as a whole.** One bad item gets `status: "error"`
  and an error object; the response stays `200`.
- **Endpoint work lives in `app/services/pipelines.py`, not in the route.** Routes
  translate and delegate. Synchronous requests and background jobs call the *same*
  function — never fork the logic, or the two paths will drift.

## How to extend

### Add an LLM provider

If it speaks OpenAI's wire format — most do — it is **one row** in
`app/services/llm/specs.py`:

```python
(_spec("acme", "Acme AI", "https://api.acme.ai/v1"),)
```

That is the whole change. The provider key doubles as the keyring service name,
so users store the key at `connections/acme`. The parametrised sweep in
`tests/unit/test_openai_compatible.py` picks the row up automatically and will
fail if it is malformed.

If the vendor wants its key somewhere unusual, set `auth=AuthStyle.HEADER` and
`auth_header=...` — that drives `scripts/provision_keyring.py`, which is what
makes the stored credential come back in the right shape.

If the API is genuinely different, write a module under
`app/services/llm/providers/` satisfying the `LLMProvider` protocol and register
it in `bootstrap.build_llm_providers`.

### Teach the service about a new model's quirks

Add a rule to `app/services/llm/capability_rules.py`, ordered most-specific
first, then a test in `tests/unit/test_capabilities.py` pinning the exact wire
body. Do **not** special-case a model inside a provider adapter — that is what
the capability layer is for.

### Add a search backend

Implement the `SearchBackend` protocol in `app/services/search/`, then add it to
`bootstrap.build_search_backends` in preference order. Raise `SearchBlockedError`
when the engine refuses to serve results so the router fails over.

## Testing

| Layer | Approach |
|---|---|
| Pure logic (cleaner, truncator, parser, capabilities) | Direct unit tests, plus hypothesis for invariants |
| HTTP clients (`httpx`) | `respx` transport mocks |
| Anthropic / OpenAI SDKs | **A real local HTTP server** (`tests/mock_api_server.py`) — those SDKs run on `httpx2`, which `respx` cannot patch. Stubbing SDK methods would test the mocks, not the request shaping. |
| Playwright | Real headless Chromium against a **local fixture server**, never Google |
| Endpoints | `httpx.ASGITransport` with fakes injected through `deps` |
| The provider fleet | One parametrised sweep over `PROVIDER_SPECS` |
| Keyring | `tests/fake_keyring.py` — **real** RS256 signing and a real JWKS, because verification is a security control and a stubbed verifier proves only that the stub works |

Google SERP fixtures live in `tests/fixtures/html/` and cover the modern layout,
the legacy layout, a consent wall, a CAPTCHA interstitial and a no-results page.
When Google changes its markup, update the fixture and the parser together.

## Gotchas

- **Model ids split on the first colon only.** `ollama:llama3.1:8b` is provider
  `ollama`, model `llama3.1:8b`. `split_model_id` handles this; do not use
  `str.split(":")`.
- **The Anthropic SDK dropped `temperature`** from `messages.create()`. Models
  that still accept it get it via `extra_body`.
- **`filterwarnings = ["error"]`** is set. A leaked socket or an unawaited
  coroutine fails the suite rather than printing a warning.
- **Playwright's bundled Chromium may not match the installed browser.**
  `resolve_executable_path()` finds a system Chromium; in this container that is
  `/opt/pw-browsers/chromium`. Never run `playwright install` here.
- **An empty string disables a provider**; `None` means "not specified, use the
  default". This applies to both `api_key` and `base_url`.
- **`asyncio.create_task` results must be referenced.** A task held only by the
  event loop can be garbage-collected mid-flight. `JobRunner` keeps its own
  reference in `self._tasks` and discards it in a done-callback; there is a test
  that forces a collection to prove it.
- **Credentials are per caller, resolved per call.** There is no env fallback and
  no global key. `GET /v1/models` is a different answer for different people, and
  `tests/unit/test_registry_keyring.py` has the isolation test that pins it.
- **Background jobs are in-process.** They are lost on restart and are not shared
  between replicas. Swap `JobStore` for a shared implementation if that matters —
  the seam exists for exactly that.
