# API reference

Interactive docs are served at `/docs`; the OpenAPI schema at `/openapi.json`.
This page covers the semantics those cannot express.

All errors are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem
documents with content type `application/problem+json`:

```json
{
  "type": "https://web-search-api.dev/problems/forbidden_url_error",
  "title": "Blocked address",
  "status": 403,
  "detail": "10.0.0.1 resolves to a private address.",
  "code": "forbidden_url_error",
  "instance": "/v1/scrape"
}
```

| Code | Status | Meaning |
|---|---|---|
| `validation_problem` | 400/422 | Request was malformed or semantically invalid |
| `auth_error` | 401 | Missing or wrong API key |
| `forbidden_url_error` | 403 | Blocked by SSRF policy or robots.txt |
| `not_found_error` | 404 | Unknown model, provider or route |
| `rate_limited_error` | 429 | An upstream rate limit was hit |
| `upstream_error` | 502 | A site or provider failed |
| `search_blocked_error` | 502 | The engine served a consent wall or CAPTCHA |
| `provider_unavailable_error` | 503 | No provider or backend is reachable |
| `timeout_problem` | 504 | An upstream call exceeded its deadline |

---

## `GET /health`

Liveness. Does no I/O, so a slow dependency never triggers a restart.
Also available at `/healthy`.

## `GET /health/ready`

Readiness. `ready` is true only when a browser is available **and** at least one
LLM provider is reachable. Returns 200 either way; read the body.

## `GET /v1/models`

Query parameter `refresh=true` re-probes instead of using the cached catalogue.

Only models from `available` providers appear. Every model carries the
capabilities the service resolved for it, so a client can tell in advance
whether `temperature` will be honoured.

## `POST /v1/search`

| Field | Default | Notes |
|---|---|---|
| `queries[]` | required | 1–20 items |
| `queries[].query` | required | 1–500 chars, non-blank |
| `queries[].max_results` | `10` | 1–50 |
| `queries[].site` | — | Adds a `site:` filter |
| `queries[].additional_notes` | — | Overrides the batch-level notes for this query |
| `fetch_pages` | `false` | Also scrape the top result pages |
| `max_pages` | `3` | 1–10, only meaningful with `fetch_pages` |
| `summarize` | `true` | |
| `model` | server default | Namespaced `provider:model` |
| `additional_notes` | — | Applied to every query in the batch |
| `language` / `region` | `en` / `us` | |
| `safe_search` | `true` | |

Each item in `results[]` has `status` of `ok` or `error`. A failing query does
not fail the batch.

Without `fetch_pages`, summaries are built from titles and snippets — fast and
cheap. With it, the top `max_pages` results per query are scraped and their full
text is summarised instead. A page that cannot be fetched keeps its snippet.

## `POST /v1/scrape`

| Field | Default | Notes |
|---|---|---|
| `urls[]` | required | 1–20 items |
| `render_js` | `auto` | `auto`, `always` or `never` |
| `summarize` | `true` | |
| `summarize_together` | `false` | One summary across all URLs instead of one each |
| `model` | server default | |
| `additional_notes` | — | |

`auto` fetches over plain HTTP and escalates to a headless browser only when the
extracted text looks like a client-rendered shell. If rendering then fails, the
HTTP text is kept rather than losing the page.

With `summarize_together`, the combined summary appears at the top level and
per-item summaries are `null`.

## `POST /v1/summarize`

Text in, summary out — a direct hook for an MCP server or another service.

| Field | Default | Notes |
|---|---|---|
| `text` | required | Non-empty |
| `model` | server default | |
| `additional_notes` | — | |
| `topic` | — | What the reader was looking for |
| `sources[]` | `[]` | URLs listed to the model for context |

## The summary object

```json
{
  "executive_summary": "Two to five sentences of prose.",
  "key_points": ["...", "..."],
  "model": "anthropic:claude-opus-5",
  "provider": "anthropic",
  "usage": {"input_tokens": 4210, "output_tokens": 180},
  "truncated": true,
  "chars_submitted": 40000,
  "original_chars": 128400,
  "notes_applied": true,
  "param_adjustments": ["temperature dropped: rejected by this model"]
}
```

`truncated`, `chars_submitted` and `original_chars` always tell you how much of
the source the model actually saw. `param_adjustments` records anything the
capability layer had to drop, rename or translate for the chosen model.

## Authentication

Off unless `WSA_API_KEYS` is set. When enabled, send `X-API-Key: <key>` or
`Authorization: Bearer <key>`. Health endpoints remain public.
