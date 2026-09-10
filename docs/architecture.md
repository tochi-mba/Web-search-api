# Architecture

## How a request flows

```
POST /v1/search
  │
  ├─ middleware        request id, timing, optional API-key auth
  ├─ pydantic          strict validation (extra="forbid")
  │
  ├─ SearchRouter      preferred backend first, failover on block/error
  │    └─ GoogleSearchBackend
  │         └─ PlaywrightBrowserSession   fresh context per navigation
  │              └─ serp_parser           pure HTML → [SearchResult]
  │
  ├─ PageFetcher       (only when fetch_pages: true)
  │    ├─ urlsafety    SSRF guard, re-run on every redirect hop
  │    ├─ robots       per-host, cached
  │    ├─ HttpFetcher  plain GET, size/redirect/content-type limits
  │    └─ extractor    HTML → title/author/date/text
  │
  └─ Summarizer
       ├─ registry.resolve      provider:model → a reachable ModelInfo
       ├─ capabilities          what this model actually accepts
       ├─ truncate              min(hard limit, model context budget)
       ├─ prompts               system + user, notes as guidance not facts
       └─ provider.chat         shaped request, one self-healing retry
```

## Layers

**`app/core/`** — infrastructure with no domain knowledge: structured logging
with request correlation, the RFC 9457 error model, bounded concurrency, a TTL
cache.

**`app/services/`** — the actual work, with no knowledge of HTTP. Every service
takes its collaborators by constructor injection, which is what makes the test
suite fast and deterministic.

**`app/api/`** — the HTTP boundary. Routes translate between pydantic schemas
and service dataclasses and do nothing else. `deps.py` is the only place routes
reach into application state.

**`app/bootstrap.py`** — the composition root. Every long-lived object is built
here from settings, so the wiring can be tested without starting a server and
there is exactly one place to look when asking "where does this come from?".

## Key design decisions

### Search is a protocol, not a Google scraper

Google serves consent walls and CAPTCHAs to headless browsers. Pretending
otherwise would produce a service that silently stops working. Both
interstitials are detected explicitly, raised as `SearchBlockedError`, and the
router fails over. A deployment that cannot rely on scraping points at SearxNG
or Serper without anything above the backend layer changing.

### The capability layer sits between the request and every adapter

Models within one provider are not interchangeable. Rather than scatter
`if model.startswith("o3")` through the adapters, one module resolves what a
model accepts and shapes the outgoing body. Adapters stay simple; new model
quirks are a table row and a test.

### Credentials belong to callers, not to this service

Providers hold no keys. A `ResolvedAuth` — headers and query parameters resolved
from keyring for one person — is passed into every `list_models` and `chat` call.
One provider object therefore serves every caller and holds nobody's secret, and
the catalogue is cached per verified account id rather than globally.

The cost is a coupling: with no environment fallback, keyring being down means no
credentialed provider works. That was chosen deliberately; `docs/keyring.md`
states the consequences plainly.

### Probing, not configuration, decides what is available

`GET /v1/models` reports what responded to a live probe. Trusting configuration
would mean a typo'd key surfaces as a 500 halfway through a user's request
instead of as an obvious `unauthorized` in the catalogue.

### Truncation is always reported

Silently summarising half a document and presenting the result as complete is a
correctness bug, not a performance optimisation. `truncated`, `chars_submitted`
and `original_chars` appear on every summary.

### Batch failure is per-item

A single dead link in a batch of twenty should cost the caller that one link,
not the other nineteen. Each item carries its own status; the response is 200.

### Background jobs run the same code as synchronous requests

Each endpoint's work lives in `app/services/pipelines.py` as a plain function.
The route either awaits it or hands it to the `JobRunner`. Two implementations
of the same operation would drift, and only one would end up properly tested;
an integration test asserts both paths produce identical output.

`JobStore` is a protocol with an in-memory implementation. That keeps the
service dependency-free, at the cost of jobs being confined to the process that
accepted them — lost on restart, invisible to other replicas. A shared
implementation slots in behind the protocol without touching the runner or the
routes.

Background work is bounded by its own semaphore, separate from the request
concurrency limit, so a queue of jobs cannot starve synchronous callers or
exhaust the shared browser.

## Request lifecycle resources

One `httpx.AsyncClient` and one browser process are shared across all requests
and owned by the lifespan. The browser hands out a fresh context per navigation
so cookies and storage never leak between unrelated requests.
