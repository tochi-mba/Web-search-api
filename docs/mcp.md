# Fronting this as MCP tools

Nothing MCP-specific is implemented. The HTTP surface is already the shape a wrapper
needs: summaries on every route, partial success on batches, and jobs that a tool can
wait on instead of polling.

Assign stable `operation_id`s before wrapping. Most OpenAPI-to-MCP bridges name tools
from those ids, and renaming one later is a breaking change for every bound client.

## The rule that comes first: page contents are data, never instructions

**Everything a search, scrape or summary returns is untrusted text.** It arrived from the
open web, or from a model that just read the open web. Ordinary prompt injection lasts
one turn. A tool result that is concatenated into the system prompt lasts until the
conversation ends, and the model cannot tell it from the person's own words.

The API cannot enforce the mitigation. **You can.**

### Do this

Render every result as a **third-person reported claim**, with its URL or query inline,
inside a frame that is clearly not the instruction block. If the token budget is tight,
return **fewer pages** — that is what `max_pages` and `wait_seconds` exist to bound —
never the same pages with the provenance stripped.

### Do not do this

- **Do not paste scrape bodies into a system prompt.** That is the attack, executed by
  the wrapper, on the person's behalf.
- **Do not let a page change tool behaviour.** A site saying "always call search again
  with this query" must be as inert as a site saying "prefers tea".
- **Do not follow redirects the service refused.** SSRF protection stays on. A wrapper
  that fetches a `Location` the service blocked is a wrapper that undid the guard.

## The tools, and when to call each

| Tool | When |
| --- | --- |
| `search` | Find pages. Summaries without `fetch_pages` are titles and snippets; with it, the top pages are scraped. |
| `scrape` | The caller already has URLs. |
| `summarize` | The caller already has the text. |
| `list_models` | Before naming a `provider:model`. The catalogue is per caller. |
| `GET /v1/jobs/{job_id}` | After `async: true`. Pass `wait_seconds` (0–60) instead of looping. |

A per-item failure inside a batch is **not** a job failure. The job succeeds and the
failure appears in `result`. A wrapper that treats any `error` object as "the tool
failed" will retry a batch that already answered honestly.

## Wrapping it

FastMCP pointed at `/openapi.json` with an **allowlist** of search, scrape, summarize,
models, and jobs. A denylist means the next endpoint is exposed by default.

`wait_seconds` is the shape a tool actually wants. A bridge that strips query
parameters and forces the model to poll will spend tokens on `GET` loops this service
already collapsed into one call.

## Known gaps

**No stable `operation_id`s yet.** Add them, pin them with a contract test, then wrap.

**No `Idempotency-Key` on submits.** Assistants retry, and a retried `async: true` starts
a second job.
