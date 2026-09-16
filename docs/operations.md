# Operations

## Running it

```bash
make run                                   # dev, :8006, reload
uv run uvicorn app.main:app --port 8006    # production entry point
make docker && docker run --rm -p 8006:8006 --env-file .env web-search-api:local
```

`GET /healthy` (alias `/health`) reports that the process is up and does no I/O at all.
Point a container's liveness probe at it. `GET /ready` (alias `/health/ready`) probes
dependencies and answers `503` when one is unusable. Point a load balancer at that one.

Nothing here needs keyring to start. With `WSA_KEYRING_BASE_URL` empty the service runs in
**anonymous mode**: no caller token can be verified, and only providers that need no
credential — Ollama and the other local runtimes — are usable. That is a deliberate mode,
not a degraded one.

## Configuration

Every setting is an environment variable prefixed `WSA_`. **Unknown variables under the
prefix are rejected at startup**, so a typo fails loudly instead of leaving a default
silently in place. `WSA_ENABLED_PROVIDERS` and `WSA_MAX_CONCURRENCY_PER_HOST` were never
implemented and are named explicitly as removed.

There are **no provider API keys here**. Every third-party credential lives in keyring and
is resolved per request, for the person the request is being made for. See
[keyring.md](keyring.md).

### Service

| Variable | Default | Notes |
| --- | --- | --- |
| `WSA_SERVICE_NAME` | `web-search-api` | Reported by `/health` and in log records. Not the keyring audience — that is `WSA_KEYRING_SERVICE_NAME`. |
| `WSA_LOG_LEVEL` | `INFO` | |
| `WSA_JSON_LOGS` | `true` | `false` for human-readable local output. |
| `WSA_API_KEYS` | *(empty)* | Comma-separated. Empty means the API is unauthenticated. See [the API-key gate](#the-api-key-gate). |

### Identity and keyring

This service authenticates nobody itself. A caller presents a token keyring signed, and it
is verified locally against keyring's published keys. See
[ADR-0004](adr/0004-local-jwks-verification.md).

| Variable | Default | Notes |
| --- | --- | --- |
| `WSA_KEYRING_BASE_URL` | *(empty)* | Where the credential vault is. **Empty is anonymous mode**: nothing can be verified and only credential-free runtimes work. |
| `WSA_KEYRING_SERVICE_TOKEN` | *(empty)* | This service's entry, under `web-search-api`, in keyring's `KEYRING_SERVICE_TOKENS` JSON map. At least 32 characters, the rule keyring enforces. Held as a `SecretStr`. Empty also means anonymous mode. |
| `WSA_KEYRING_ISSUER` | `http://127.0.0.1:8001` | Must equal keyring's own `KEYRING_ISSUER`, or every token is refused. |
| `WSA_KEYRING_SERVICE_NAME` | `web-search-api` | The audience callers mint tokens for, and this service's key in `KEYRING_SERVICE_TOKENS`. Validated as an exact audience at startup. |
| `WSA_KEYRING_DEFAULT_PROFILE` | `personal` | Credential set used when a request sends no `X-Keyring-Profile`. With settings-api in use, each person's own default takes its place. |
| `WSA_KEYRING_TIMEOUT_SECONDS` | `10.0` | Per call to keyring, and the deadline for fetching its signing keys. `>0`. |
| `WSA_JWKS_CACHE_SECONDS` | `3600.0` | How long keyring's public keys are held before re-reading. `≥0`. |
| `WSA_JWKS_MIN_REFETCH_SECONDS` | `60.0` | Floor between the key refetches an unknown key id may provoke. Not a tuning knob — see ADR-0004. `>0`. |
| `WSA_JWKS_STALE_GRACE_SECONDS` | `86400.0` | How long past the cache period held keys are still served while keyring is unreachable. `≥0`. |

Keyring counts as configured only when **both** the base URL and the service token are set.
Setting one without the other leaves the service in anonymous mode, and a caller who then
presents a token gets a `503` saying keyring is not configured — never a `401`, because the
token was never the problem.

Two settings must agree with keyring or nothing works, and both fail closed:
`WSA_KEYRING_ISSUER` and `WSA_KEYRING_SERVICE_NAME`. A mismatch refuses every token with
the same message every other bad token gets, so check these first when a working deployment
suddenly accepts nobody.

### Per-person settings (settings-api)

Unset, everybody gets the configuration on this page — which is what this service did
before it read anybody's settings at all, and how a deployment ships. Set both variables
and each request reads that person's `search` settings: their default model, content
ceiling, preferred search backend, disabled providers, job retention, and which profile
they mean when they name none.

| Variable | Default | Notes |
| --- | --- | --- |
| `WSA_SETTINGS_API_BASE_URL` | *(unset)* | Where settings-api is. Blank or unset is off. |
| `WSA_SETTINGS_API_TOKEN` | *(unset)* | This service's entry in settings-api's `SETTINGS_API_SERVICES`. At least 32 characters. Its grant there needs `audience_prefix` `web-search-api`: settings-api is shown the same user token keyring is. |

**The pair must be set together.** A URL with no token would be refused on every call; a
token with no URL is a secret configured for nothing. Either is a startup error.

A person may lower `max_content_chars` and add to `disabled_providers`; they can never
raise the ceiling or re-enable a provider the operator turned off. Job retention is theirs,
within the bounds settings-api enforces, and is computed when a job is submitted and stored
on that job — the reaper has no user token to ask with.

Nothing is fetched at startup, so an unreachable settings-api cannot stop this service
coming up. During an outage most settings fall back to the configuration, but two refuse
rather than be guessed: `disabled_providers`, because guessing the empty list would send a
query to a provider that person refused, and `default_profile`, because guessing would
quietly act on the wrong account. Each fails only the operation that actually needs it — a
scrape that never summarises still runs.

### Providers and models

| Variable | Default | Notes |
| --- | --- | --- |
| `WSA_DEFAULT_MODEL` | `anthropic:claude-opus-5` | Namespaced `provider:model`, used when a caller names none. If it is unreachable, the first available model is used instead rather than failing. |
| `WSA_MODEL_CACHE_TTL_SECONDS` | `300.0` | How long a probed catalogue stays fresh. Cached per caller, never globally. `≥0`. |
| `WSA_PROVIDER_PROBE_TIMEOUT_SECONDS` | `5.0` | Per-provider deadline while building a catalogue. A slow provider drops out; it does not hold up the rest. `>0`. |
| `WSA_MAX_CONTENT_CHARS` | `40000` | Hard ceiling on scraped text handed to a model. The effective limit is the smaller of this and the model's context budget. `>0`. |
| `WSA_PROVIDER_BASE_URLS` | `{}` | JSON object keyed by provider, e.g. `{"ollama":"http://gpu:11434"}`. An explicit `""` disables that provider; omitting a key means "use the default". |
| `WSA_DISABLED_PROVIDERS` | *(empty)* | Comma-separated provider keys that are never probed and never used. A person may add to this list, never subtract from it. |

`GET /v1/models` reports what responded to a live probe **for the caller who asked**, so it
is a different answer for different people. See
[ADR-0002](adr/0002-providers-are-adapters-behind-a-registry.md).

### Fetching

| Variable | Default | Notes |
| --- | --- | --- |
| `WSA_REQUEST_TIMEOUT_SECONDS` | `20.0` | Per page fetch and per search backend call. LLM adapters get three times this, because generation is slower than fetching. `>0`. |
| `WSA_MAX_REDIRECTS` | `5` | Redirect budget per fetch. Every hop is re-checked by the SSRF guard. `≥0`. |
| `WSA_MAX_RESPONSE_BYTES` | `5000000` | Body bytes kept; anything beyond is discarded and the truncation logged. `>0`. |
| `WSA_USER_AGENT` | a Chrome 131 string | Sent on page fetches, on robots.txt, and by browser contexts. |
| `WSA_RESPECT_ROBOTS` | `true` | Honour robots.txt. Fetched once per host and cached for an hour. |
| `WSA_ALLOW_PRIVATE_NETWORKS` | `false` | Permit private and loopback addresses, for deployments crawling their own infrastructure. **Cloud metadata hosts stay blocked even then.** |
| `WSA_MAX_CONCURRENCY` | `8` | Global in-flight limit for batch work, and how many providers are probed at once. `>0`. |

The SSRF guard is not optional and is the reason this service can be pointed at
caller-supplied URLs at all. See [security.md](security.md) and
[ADR-0001](adr/0001-ssrf-is-the-threat-model.md).

### Search and the browser

| Variable | Default | Notes |
| --- | --- | --- |
| `WSA_SEARCH_BACKEND` | `google` | Which backend to try first: `google`, `searxng` or `serper`. An unknown name degrades to the default order rather than taking search down. |
| `WSA_SEARXNG_BASE_URL` | *(empty)* | A URL, not a secret. Empty means SearxNG is skipped during failover. |
| `WSA_BROWSER_HEADLESS` | `true` | Set `false` when hunting for selectors. |
| `WSA_BROWSER_NAVIGATION_TIMEOUT_MS` | `20000` | Per navigation, and per wait for the results selector. `>0`. |
| `WSA_BROWSER_EXECUTABLE_PATH` | *(unset)* | A Chromium to launch instead of Playwright's own. Read straight from the environment rather than from `Settings`, and checked before the well-known system paths. |

Serper has no setting of its own: its API key is a keyring credential like any other,
stored under the service name `serper`, and resolved for the caller making the request. A
deployment can therefore run search on one person's Serper account without that key sitting
in this service's environment.

### Background jobs

| Variable | Default | Notes |
| --- | --- | --- |
| `WSA_MAX_BACKGROUND_JOBS` | `4` | Jobs running at once. Bounded separately from request concurrency so a queue of jobs cannot starve synchronous callers or exhaust the shared browser. `>0`. |
| `WSA_JOB_RETENTION_SECONDS` | `3600.0` | How long a finished job stays readable. `0` disables expiry. `≥0`. |
| `WSA_MAX_STORED_JOBS` | `1000` | Hard cap before the oldest *finished* jobs are evicted. Running jobs are never evicted. `>0`. |

Jobs are held in memory by the process that accepted them. They are lost on restart and are
not shared between replicas.

### The API-key gate

`WSA_API_KEYS` is a comma-separated list. Empty — the default — means the API is
unauthenticated. Set it and every route except `/health`, `/healthy`, `/ready`,
`/health/ready`, `/docs`, `/redoc` and `/openapi.json` requires `X-API-Key`.

This is a **network-level gate, not identity**. It names no person and resolves no
credential. The keyring user token is the real identity, and a valid API key does not
substitute for one. Health routes stay public so probes keep working without holding a
secret.

## Readiness

`GET /ready` reports two components:

- **browser** — whether a browser *could* be launched, not whether one is running. The
  browser starts lazily on first use.
- **llm** — whether any provider is configured, with how many of them answer without a
  credential and how many models an anonymous probe could list.

The probe acts for nobody, so it carries no credential. Most providers need a per-caller
credential from keyring, so requiring a *model* here would report a perfectly healthy
cloud-only deployment as permanently unready; whether one caller's credential works is
answered per request, not by a probe. A settings-api outage does not fail it either: it
applies no person's disabled-provider list, so there is nothing to guess at.

## Deploying

The image is built on `mcr.microsoft.com/playwright/python`, which already carries a
matching Chromium and its system libraries — far less fragile than installing them by hand,
and the reason `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` is set. It runs as the unprivileged
`pwuser` the base image provides, binds `0.0.0.0:8006`, and exposes that port. There is no
`HOST` or `PORT` setting; change the bind in a reverse proxy, not here.

The container `HEALTHCHECK` probes **`/health`**, not `/ready`, so a keyring blip or an
unreachable provider does not restart the container.

A load balancer should use **`/ready`**: it answers 503 only when this process cannot serve
anybody — no provider configured, or no browser available — which is exactly when traffic
should go elsewhere. Two further things decide the shape of a deployment:

1. **Background jobs live in the process that accepted them.** Behind a load balancer a
   client polling `/v1/jobs/{id}` must reach the instance it submitted to. Run a single
   replica, or use sticky sessions, or do not use `async` mode.
2. **One browser process is shared by every request in a replica**, handing out a fresh
   context per navigation. Memory is dominated by that browser, not by the API.

## Exposing it

This service is built to be reachable by a handful of people. It is not built to be
reachable by everyone. Work down this list before it is:

1. **Terminate TLS at a reverse proxy** (Caddy, nginx, Traefik) and let it hold the
   certificate. This service speaks plain HTTP and should sit behind the proxy. A bearer
   token over plain HTTP is a bearer token in somebody's transit logs.
2. **Set both keyring settings from a secret store, not from a shell.**
   `WSA_KEYRING_SERVICE_TOKEN` is a shared secret; it is held as a `SecretStr` so it cannot
   be printed with the rest of the settings, and it should not reach a shell history either.
3. **Decide about `WSA_ALLOW_PRIVATE_NETWORKS` deliberately.** The default is the right
   answer for anything reachable from outside.
4. **Consider blocking `/docs`, `/redoc` and `/openapi.json` at the proxy.** They are open
   so the interactive docs work in a browser. They describe the service rather than anybody
   using it, but there is no reason to publish even that.

What the service does for itself:

- **Caller-supplied URLs are checked against resolved addresses**, on the first request and
  again on every redirect hop. See [ADR-0001](adr/0001-ssrf-is-the-threat-model.md).
- **Credentials are resolved per call and stored nowhere** — not on a provider, not on a
  job, not in a log record, not in any response. The catalogue cache is keyed on the
  verified account id, never on anything a caller supplies.
- **Every rejection of a token says the same thing.** Which check failed goes to the log,
  where an operator reads it and a forger does not.
- Unexpected error text is never returned to callers; it can carry URLs or credentials.
  Callers get a request id that ties the response to the full log record.

What remains your problem:

- **Scraped pages are untrusted text that reaches a language model.** The prompt fences
  them and says so, but treat summaries of untrusted pages as untrusted output. Do not feed
  them into anything that acts without review.
- **Jobs are in-memory.** A restart forgets them.
- **Scraping is subject to the target site's terms of service**, and that is the operator's
  responsibility, not the tool's.

## What each failure means

| Symptom | Likely cause |
| --- | --- |
| Startup fails naming an unknown setting | A typo under `WSA_`. The message names the variable and never its value. |
| Startup fails naming `WSA_ENABLED_PROVIDERS` or `WSA_MAX_CONCURRENCY_PER_HOST` | Both were never implemented. Rejected on purpose, rather than accepted and silently ignored. |
| Startup fails on a short service token | `WSA_KEYRING_SERVICE_TOKEN` or `WSA_SETTINGS_API_TOKEN` is under 32 characters, the rule keyring and settings-api enforce on their side. |
| Startup fails: base URL and token must be set together | Half a settings-api configuration. Set both or neither. |
| Every request answers `401 auth_error` on a deployment that worked | `WSA_KEYRING_ISSUER` or `WSA_KEYRING_SERVICE_NAME` no longer matches keyring. Both fail closed and say nothing more. |
| A token gets `503` saying keyring is not configured | A user token arrived at a deployment in anonymous mode. Set both keyring variables, or stop sending a token. |
| `503` naming keyring unreachable | Keyring's signing keys cannot be fetched. Cached keys survive a bounded outage; a cold start does not. |
| `/ready` is `503` with "no reachable LLM provider" | Readiness probes anonymously, so only credential-free runtimes can satisfy it. Expected on a cloud-only deployment — use `/health` for liveness. |
| A provider shows `not_configured` with a token present | That account has not connected it on that profile. Check with `scripts/provision_keyring.py --check`. |
| A provider shows `unauthorized` | Reachable, but it rejected the stored credential — usually a key stored under the wrong header. Re-run the provisioning script, which sets it correctly. |
| A provider shows `unreachable` | It timed out, refused the connection or errored within `WSA_PROVIDER_PROBE_TIMEOUT_SECONDS`. |
| `404 not_found_error` on a model you hold a key for | The wrong profile, or the provider dropped out of the catalogue. Check `X-Keyring-Profile`. |
| `403 forbidden_url_error` | The SSRF guard or robots.txt refused that URL. The detail says which. |
| `502 search_blocked_error` | Google served a consent wall or CAPTCHA, and no other backend was configured or they also failed. Configure SearxNG or Serper to fail over to. |
| `503` "No search backend configured" | `google` needs a working browser, `searxng` needs `WSA_SEARXNG_BASE_URL`, and `serper` needs a credential this caller has connected. |
| `503` naming settings | settings-api refused this service — a grant or token it will not accept — or a setting that must not be guessed could not be read. Name a profile in the request, or check this service's grant. |
| Jobs vanish after a restart | Expected — the job store is in-memory. |
| Polling a job id returns `404` | It expired after `WSA_JOB_RETENTION_SECONDS`, was evicted once `WSA_MAX_STORED_JOBS` was reached, or the client reached a different replica than the one it submitted to. |
| Summaries cover less than the page | Truncation, always reported: `truncated`, `chars_submitted` and `original_chars` say exactly how much the model saw. |
| The browser fails to launch | Playwright's bundled Chromium does not match the installed browser, which is common in containers. Set `WSA_BROWSER_EXECUTABLE_PATH`. |
| Logs are not JSON despite `WSA_JSON_LOGS=true` | The process was started before the setting was applied; logging is configured in `create_app`. Restart. |
