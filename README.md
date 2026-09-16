# web-search-api

A FastAPI service that scrapes Google search results and web pages, then
synthesises an **executive summary** with any of ~60 LLM providers — local
Ollama, every Anthropic and OpenAI model, and the whole OpenAI-compatible
ecosystem.

API only. No UI. Designed to sit behind an MCP server.

```bash
make install                      # uv sync --all-extras
cp .env.example .env              # point it at your keyring
make run                          # http://localhost:8006/docs
```

**Provider keys do not live here.** Every third-party credential is stored in
[keyring](https://github.com/tochi-mba/keyring-api) and resolved per request for
the person the request is being made for — so answers are per caller. See
[docs/keyring.md](docs/keyring.md). Local model runtimes need no credential and
work without keyring at all.

---

## What it does

| Endpoint | Purpose |
|---|---|
| `GET /health` (alias `/healthy`) | Liveness. Does no I/O. |
| `GET /ready` (also `/health/ready`) | Readiness: 200 when ready, 503 when dependencies are degraded. |
| `GET /v1/models` | Models reachable **right now**, with per-model capabilities. |
| `POST /v1/search` | Batch Google queries → results (+ optional deep fetch) + summary. |
| `POST /v1/scrape` | Batch URLs → clean extracted text + summary. |
| `POST /v1/summarize` | Text in, summary out. A direct hook for an MCP server. |
| `GET /v1/jobs`, `GET|DELETE /v1/jobs/{id}` | Poll, list and cancel background jobs. |

Interactive docs at `/docs`, OpenAPI schema at `/openapi.json`.

### Search

```bash
curl -s -X POST localhost:8006/v1/search \
  -H 'content-type: application/json' -d '{
    "queries": [
      {"query": "widget latency causes", "max_results": 10},
      {"query": "dispatch queue sharding", "site": "reddit.com",
       "additional_notes": "Focus on production incidents, not theory."}
    ],
    "fetch_pages": true,
    "max_pages": 3,
    "model": "anthropic:claude-opus-5",
    "additional_notes": "The reader is an SRE deciding whether to shard."
  }' | jq
```

By default the summary is built from result titles and snippets, which is fast
and cheap. `fetch_pages: true` additionally scrapes the top `max_pages` result
pages and summarises their full text.

### Scrape

```bash
curl -s -X POST localhost:8006/v1/scrape \
  -H 'content-type: application/json' -d '{
    "urls": ["https://example.com/article"],
    "render_js": "auto",
    "summarize_together": false,
    "additional_notes": "Pull out any figures."
  }' | jq
```

`render_js` is `auto` (plain HTTP, escalating to a headless browser when the
result looks like a client-rendered shell), `always`, or `never`.

### Background mode

A twenty-URL scrape with summarisation takes minutes. Rather than hold the
connection open, pass `async` (or `background`) and get an immediate `202` with
a job id to poll:

```bash
curl -sD- -X POST localhost:8006/v1/scrape \
  -H 'content-type: application/json' \
  -d '{"urls": ["https://example.com"], "async": true}'
# HTTP/1.1 202 Accepted
# Location: /v1/jobs/f8c0340681104c648c7e9e860d8b4af6
# Retry-After: 2

curl -s localhost:8006/v1/jobs/f8c0340681104c648c7e9e860d8b4af6
# {"status": "running",   "result": null, ...}
# {"status": "succeeded", "result": {"results": [...]}, ...}
```

Works on `/v1/search`, `/v1/scrape` and `/v1/summarize`. `GET /v1/jobs` lists
recent jobs, `DELETE /v1/jobs/{id}` cancels one that is still running.

**The `result` is exactly what the synchronous call would have returned** — the
two modes run the same code, and a test asserts the outputs are identical. So
switching a client to background mode never means reshaping how it reads the
answer.

> **Jobs live in the process that accepted them.** They are lost on restart and
> are not visible to other replicas, so behind a load balancer a client must
> poll the instance it submitted to. `JobStore` is the seam where a shared
> backend (Redis, a database) drops in; nothing above it would change.

### Batch semantics

One bad URL or blocked query **never** fails the batch. Every item carries its
own `status` (`ok` / `error`) and a structured `error` object, and the response
is still `200`. Deep page fetching is enrichment: a page that cannot be fetched
simply keeps its snippet.

---

## Model selection

Models are namespaced `provider:model` — `anthropic:claude-opus-5`,
`openai:gpt-4o`, `ollama:llama3.1:8b`. The split is on the **first colon only**,
because Ollama tags legitimately contain colons.

`GET /v1/models` lists only what is reachable **for you**. Present a keyring user
token and you get the providers that account has connected; without one you get
only the runtimes needing no credential. A provider you have not connected, or
whose credential no longer works, is reported with a status but contributes no
models, so anything listed can actually be used:

```json
{
  "default_model": "anthropic:claude-opus-5",
  "models": [
    {
      "id": "anthropic:claude-opus-5",
      "provider": "anthropic",
      "model": "claude-opus-5",
      "context_window": 1000000,
      "capabilities": {
        "supports_temperature": false,
        "supports_json_mode": true,
        "reasoning": "adaptive_thinking",
        "max_tokens_param": "max_tokens"
      }
    }
  ],
  "providers": [
    {"name": "anthropic", "status": "available",    "model_count": 9},
    {"name": "openai",    "status": "unauthorized", "detail": "invalid api key"},
    {"name": "groq",      "status": "not_configured"}
  ]
}
```

Statuses are `available`, `unauthorized`, `unreachable` and `not_configured`.
`?refresh=true` re-probes instead of using the cache.

### Why models are not interchangeable

Models **within one provider** accept different parameters. Sending one request
shape per provider would 400 on a large slice of the catalogue:

| Family | Constraint |
|---|---|
| OpenAI `o1`/`o3`/`o4`, GPT-5 | reject `temperature`; need `max_completion_tokens`; `developer` role |
| OpenAI `gpt-4o`/`gpt-4.1` | the opposite — `max_tokens` and `temperature` are fine |
| Anthropic Opus 5 / Sonnet 5 | `thinking: adaptive` + `effort`; `budget_tokens` and `temperature` are rejected |
| Anthropic Haiku 4.5 | `budget_tokens` **required** for thinking; `effort` rejected |
| DeepSeek `deepseek-reasoner` | rejects sampling parameters |
| Qwen | `enable_thinking` toggle |
| Z.AI GLM | `thinking: {"type": "enabled"}` |

`app/services/llm/capabilities.py` resolves what each model accepts — from live
provider metadata, then a model-id pattern table, then conservative defaults —
and **shapes** the request: dropping, renaming or translating as needed. Every
change is reported back in `param_adjustments`.

If a provider still rejects a parameter, the adapter strips it and retries
**once**, so a model released after this code was written degrades to a working
request instead of failing.

---

## Truncation

Scraped text is cleaned, then truncated on a word boundary to:

```
min(MAX_CONTENT_CHARS, model_context_window − prompt_overhead − reserved_output)
```

So summarising with Haiku 4.5 (200K context) truncates harder than Opus 5 (1M)
automatically. Every response reports `truncated`, `chars_submitted` and
`original_chars` — presenting a summary of half a document as complete would be
a lie.

`additional_notes` is capped separately, placed with the instructions rather
than the source text, and explicitly framed to the model as guidance about
focus rather than a source of facts.

---

## Search backends

Google actively fights scrapers: it serves consent walls, CAPTCHAs and rotating
markup to headless browsers. Both interstitials are detected explicitly and
surfaced as `SearchBlockedError`, and the router fails over to the next
configured backend rather than returning silent nonsense.

| Backend | Requires | Notes |
|---|---|---|
| `google` (default) | Chromium | Free, and the one that gets blocked. |
| `searxng` | `WSA_SEARXNG_BASE_URL` | Self-hostable metasearch. The recommended failover. |
| `serper` | a `serper` credential in keyring | Paid SERP API. Most reliable. Per caller, like every other credential. |

Set the preferred backend with `WSA_SEARCH_BACKEND`. Zero results from the last
backend is returned as a genuine empty answer, not an error.

---

## Security

The service fetches URLs chosen by its callers, which is the textbook setup for
SSRF. `app/services/urlsafety.py` rejects anything that is not a public http(s)
endpoint: private, loopback, link-local, reserved and multicast ranges, cloud
metadata hosts by name as well as by address, and hosts resolving to any blocked
address.

**The check is re-applied on every redirect hop.** Redirects are followed
manually rather than by httpx, because validating only the caller's URL is
worthless if it may redirect to `127.0.0.1` or `169.254.169.254`.

Also enforced: robots.txt (per-host, cached), response size caps, redirect
budgets, a content-type allowlist, and a global plus per-host concurrency limit.

Set `WSA_API_KEYS` to require an API key (`X-API-Key` or `Bearer`). Health
endpoints stay public so probes keep working. Note that under keyring the user
token is the real identity — a caller without one cannot resolve any credential
— so `WSA_API_KEYS` is a network-level control on top of that, not the primary one.

---

## Configuration

Every value is optional — see `.env.example` for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `WSA_DEFAULT_MODEL` | `anthropic:claude-opus-5` | Used when a caller does not choose. |
| `WSA_MAX_CONTENT_CHARS` | `40000` | Hard truncation ceiling. |
| `WSA_RESPECT_ROBOTS` | `true` | Honour robots.txt on `/v1/scrape`. |
| `WSA_ALLOW_PRIVATE_NETWORKS` | `false` | Permit private addresses. Metadata hosts stay blocked. |
| `WSA_SEARCH_BACKEND` | `google` | Preferred backend. |
| `WSA_API_KEYS` | *(empty)* | Optional front-door gate. The keyring user token is the real identity. |
| `WSA_KEYRING_BASE_URL` | *(empty)* | Where the credential vault lives. Empty means local models only. |
| `WSA_KEYRING_SERVICE_TOKEN` | *(empty)* | This service's own token, as keyring knows it. |
| `WSA_KEYRING_DEFAULT_PROFILE` | `personal` | Used when no `X-Keyring-Profile` header is sent. |
| `WSA_MAX_CONCURRENCY` | `8` | Global in-flight limit for batch work. |
| `WSA_MAX_BACKGROUND_JOBS` | `4` | Concurrent background jobs. |
| `WSA_JOB_RETENTION_SECONDS` | `3600` | How long a finished job stays readable. |
| `WSA_SETTINGS_API_BASE_URL` | *(empty)* | Where settings-api is. Unset, everybody shares the knobs above. |
| `WSA_SETTINGS_API_TOKEN` | *(empty)* | This service's token at settings-api. Must be set with the URL. |

When settings-api is configured, each caller can lower `max_content_chars`,
choose a default model and search backend, disable extra providers, pick a
default keyring profile, and choose how long their finished jobs stay
readable. See [docs/architecture.md](docs/architecture.md#per-person-settings-ship-dark).

Provider credentials are **not** environment variables. They live in keyring,
under the provider's key as the service name (`anthropic`, `groq`, …), and are
provisioned with `scripts/provision_keyring.py`. See
[docs/keyring.md](docs/keyring.md).

---

## Development

```bash
make check     # ruff, mypy, import-linter, pytest @ 100% branch coverage
make test      # tests only
make fmt       # auto-format
make run       # dev server with reload
```

Test coverage is enforced at **100%** (`fail_under = 100`). See
[`docs/testing.md`](docs/testing.md) for how each layer is tested — including
why the Anthropic and OpenAI adapters run against a real local HTTP server
rather than `respx`, and why browser tests never touch Google.

Further reading:

- [`docs/keyring.md`](docs/keyring.md) — where credentials live and how to set them up
- [`AGENTS.md`](AGENTS.md) — conventions and how to extend the codebase
- [`docs/architecture.md`](docs/architecture.md) — how a request flows through
- [`docs/operations.md`](docs/operations.md) — every setting, deploying, and what each failure means
- [`docs/providers.md`](docs/providers.md) — adding a provider (one table row)
- [`docs/security.md`](docs/security.md) — the SSRF threat model
- [`docs/testing.md`](docs/testing.md) — testing strategy
- [`docs/adr/`](docs/adr/README.md) — the decisions behind all of it, and what would change them

## Licence

MIT.
