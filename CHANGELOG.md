# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Fixed

- **A summary says which source backs each point, and a page cannot end its own source
  block.** Results reach the summariser numbered as the caller numbers them, `## [2] title`,
  and each key point ends with the numbers of the sources behind it, so a figure can be
  checked without opening every page. The block is fenced with `<source_text>` tags, and a
  tag inside the content is escaped: the old plain-text end marker could be written by a page,
  which then carried on in the instruction voice. Key points are capped at five by default,
  and each must add something the summary does not already say. The search path no longer
  lists every URL a second time.
- **Extended thinking is sent without a temperature.** On models that take a thinking
  budget (Claude Haiku 4.5 among them), a request that asked for effort carried both the
  budget and `temperature=0.2`, a pair the Messages API refuses, so the call was a 400. The
  temperature now gives way, and the adjustment says so.
- **A summary asks for no reasoning budget.** It inherited the default effort, so a
  connected reasoning model spent part of the summary's output cap thinking, and a reply cut
  off at the cap was stored raw.
- **A summary that fails costs only the summary.** A timeout, a 502 or a provider's 400 from
  the summarising model failed the whole `/v1/search` or `/v1/scrape` response, so a search
  that found its results reported none and a fetched page reported it could not be read.
  Results and pages now stand, with `summary_error` beside them saying why there is no
  summary.
- **settings-client 0.4.2.** A 2xx answer the client cannot use -- a proxy's page, an empty
  body, a document from a newer settings-api -- is treated as an outage and degrades as one,
  instead of reaching this service as a 500.
- **A person's settings are read for the profile the request runs as.** The model, backend,
  safe-search level, result count and recency are profile-scoped, and settings-api returns a
  profile's values only to a request that names the profile. This service named none, so
  each reached it as the catalogue default: a person who chose `strict` filtering got
  `moderate`. The request's `X-Keyring-Profile` is now named, or with none the person's
  `common.default_profile`, read first. settings-client moves to 0.4.0, whose test fake keeps
  profiles apart; the old one ignored them, which is why no test caught this.
- **A blocked Google search is reported as soon as the block page loads.** The render waited
  only for the results element, which a captcha or consent page does not have, so a blocked
  search sat out the whole navigation timeout (about fifty seconds end to end). A caller with
  a shorter deadline gave up first and never learned the search had been blocked. The render
  now stops at whichever of results or a block page appears first.

### Added

- **A person's language, region, blocked sites and page reading shape their searches.**
  Six `search` settings now reach the service. A search that leaves `language` or `region`
  out takes the person's `search.language` and `search.region`, then `en` and `us` as
  before. Results from a site in `search.blocked_domains`, or any of its subdomains, are
  dropped before anything is read or summarised; the rest keep the rank the backend gave
  them, and a query whose `site` names a blocked domain still gets that site's results, as
  does a URL named to `/v1/scrape`. A
  summarised search that leaves `fetch_pages` out reads the top `search.read_top_pages`
  pages (none until chosen) and writes its summary from their text. Every summary, from
  search, scrape and summarize alike, is written at the person's `search.summary_length`
  (`brief`, `standard`, `detailed`) and follows their `search.research_notes` after the
  request's own notes, inside the same 4,000-character cap; `notes_applied` still reports
  only the request's notes. `language`, `region`, `fetch_pages` and `max_pages` may now be
  omitted. With nobody's choices, and during a settings-api outage, every search and
  summary is exactly what it was. settings-client moves to 0.4.1.

- **A person's safe-search level and recency reach the search backend.**
  `search.safe_search` and `search.recency_days` could be set and read back, and changed
  nothing. A search that says nothing now takes the person's level (`off`, `moderate`,
  `strict`) and their recency. `POST /v1/search` takes `recency_days` (1 to 365), and
  `safe_search` may be omitted. A request can ask for more filtering than the person chose,
  never less, and a settings-api outage filters at `moderate`. Google has one filter, so
  both filtering levels turn it on; SearxNG is sent all three levels, and the smallest of
  day, week, month or year that covers the recency.
- A GitHub Pages site at <https://tochi-mba.github.io/Web-search-api/>, in the REX ink/signal style: what Web-search-api is,
  its API, how to run it and what it will not do. `site/` is plain static HTML;
  `.github/workflows/pages.yml` publishes it after `scripts/check_site.py` has checked every
  page for a broken anchor, a missing asset, an image without alt text or draft text.
- The repository is attributed to REX Technologies: the LICENSE copyright holder, the package
  author and the README.
- The service: `POST /v1/search` scrapes Google results (failing over to SearxNG and
  Serper when Google refuses), `POST /v1/scrape` fetches pages, rendering them in headless
  Chromium when they need it, and `POST /v1/summarize` writes an executive summary with
  any of the providers `GET /v1/models` lists. Fetches go through an SSRF guard that
  re-validates every redirect hop and honour robots.txt. Batch endpoints answer `200` with
  a per-item status; errors are RFC 9457 problem+json.
- Background jobs: `"background": true` on search, scrape or summarize answers `202`
  with a job to poll at `GET /v1/jobs/{id}`; `GET /v1/jobs` lists them and `DELETE`
  cancels one. Background and synchronous requests run the same pipeline.
- `GET /v1/jobs/{id}?wait_seconds=` (0-60) holds the request open until the job finishes,
  so a caller need not poll in a loop.
- `docs/mcp.md`: what a model may call through this service and how results are framed.
- Optional per-person settings from settings-api (namespace `search`), off by
  default. `WSA_SETTINGS_API_BASE_URL` and `WSA_SETTINGS_API_TOKEN` are both or
  neither. When set, each caller gets their own default model, content ceiling,
  preferred search backend, disabled providers, job retention and default
  profile, clamped to this deployment's caps where the catalogue says a person
  may only narrow. `disabled_providers` and `default_profile` refuse during an
  outage and fail only the operation that needs them.
- Family tooling: `make check` is `lint type imports test`, import-linter
  contracts, `.python-version`, `CLAUDE.md`, `.editorconfig`, and
  `.pre-commit-config.yaml`. Dev dependencies live in `[dependency-groups] dev`.
- `docs/operations.md`: every `WSA_` variable with its default, how anonymous
  mode and per-person settings are turned on, deploying, and what each failure
  means.
- `docs/adr/`: the SSRF threat model, providers as adapters behind a registry,
  Playwright behind one adapter, and local token verification.

### Changed

- **Breaking:** the floor is now **Python 3.12** (CI runs 3.12 and 3.13).
  `.python-version`, `requires-python`, ruff's `target-version`, mypy's `python_version`,
  the Docker base image and the pre-commit interpreter all moved together, and `uv.lock`
  was regenerated. The family-wide reason is in the meta-repo's
  [ADR-0008](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0008-python-3-12-floor.md):
  `weftai`, which the assistant hub depends on, requires 3.12 and uses PEP 695 type
  parameters that do not parse on 3.11. Generics here moved to PEP 695 syntax with it.
- **Breaking:** every credential comes from keyring, per caller. Provider keys and the
  Serper key are no longer read from the environment; each request resolves the caller's
  own, so `GET /v1/models` lists the providers that account has connected. User tokens
  are verified locally against keyring's JWKS. Background jobs resolve the credential at
  submit time. `scripts/provision_keyring.py` stores each vendor's key under the header
  it expects.
- The service listens on its family port, `8006`, and the image runs as the unprivileged
  `pwuser`. `/healthy` is liveness and does no I/O.
- keyring-client and settings-client come from their repositories' tags, so a clone, a
  CI job or an image build needs no sibling checkout. CI calls the family's shared
  service workflow.
- Image startup uses its installed dependencies without synchronizing or downloading development tools.
- The image copies `uv.lock`, builds frozen, and healthchecks `/healthy`.
- CI gets a short-lived token from the family token broker over OIDC (`id-token: write`)
  rather than inheriting a shared credential; image builds accept a BuildKit
  `github_token` secret so tagged client packages can be fetched from private family
  repositories.
  `make docker` uses the signed-in GitHub account without saving its token in an image.
- Verify caller tokens through the shared keyring client: pin issuer and audience,
  require a signing-key id, rate limit refreshes and use a bounded stale-key grace.
- Accept `Authorization: Bearer` as the canonical user-token header. The legacy
  `X-Keyring-User-Token` header remains accepted for one release; conflicting headers
  fail authentication. The optional API-key gate requires `X-API-Key` separately.
- Readiness is available at `/ready` and `/health/ready` and returns 503 when degraded.
- Reject unknown `WSA_` variables and the unused `WSA_ENABLED_PROVIDERS` and
  `WSA_MAX_CONCURRENCY_PER_HOST` settings. Configure provider URLs through
  `WSA_PROVIDER_BASE_URLS`. Validate service secrets and hide them in representations.
- Align development tooling with the family Makefile: `make check` runs lint,
  mypy, import-linter contracts and pytest at 100% branch coverage.

### Removed

- The `openai` dependency. Nothing imported it: OpenAI is a row in the spec table,
  served by the plain-`httpx` adapter like every other OpenAI-compatible vendor. A test
  now fails when a declared runtime dependency is no longer imported.
- `ProviderSpec.base_url_env`. Nine local-runtime rows named an environment variable
  (`LMSTUDIO_BASE_URL`, `VLLM_BASE_URL`, ...) that nothing read; endpoints are overridden
  through `WSA_PROVIDER_BASE_URLS`.
- Three things nothing read, found by the family dead-code sweep: the
  `ModelCapabilities.supports_top_p` flag (seven rules set it, no request carries a
  `top_p` and no adapter consulted it), `FetchResult.redirected` (the same as comparing
  `url` with `final_url`, which is what callers do) and the `I` alias in the provider
  spec table (`inference` is the default kind, so no row named it).

### Fixed

- robots.txt is fetched again once the cached copy is an hour old. It used to be cached
  for the life of the process, so a site that began refusing crawlers went on being
  crawled until a restart.
- The image's Playwright base matches the locked Playwright, and a test keeps them equal.
  They had drifted, so there was no browser to launch while `/ready` still said there
  was: the browser check now looks for the executable and reports its path.
- Documentation checked against the code: the API-key header, what `/ready` answers, every
  problem code, the provider count, the fetch and request limits, every setting in
  `.env.example`, how to get a Chromium, and where keyring-client comes from.
- The package description and the OpenAPI summary said "~60 LLM providers"; there are 54.
  The summary now derives the number from what bootstrap builds, and a test holds
  `pyproject.toml`, `README.md` and `AGENTS.md` to it.
- `/ready` counts a provider that needs a credential when keyring is configured to supply
  one. A cloud-only deployment, every local runtime turned off, answered `503` although
  every caller bringing a token was served, because the anonymous probe sees each cloud
  provider as `not_configured`.
- `scripts/smoke.py` exits non-zero and lists what failed when any call is not a 200,
  readiness is degraded, or a batch item is in error. It used to print "Smoke test
  complete." and exit 0 whatever came back, and told you to "set a provider key", which
  this service never reads. It now sends a user token (`--token` or `SMOKE_USER_TOKEN`)
  and the `X-API-Key` gate's key (`--api-key`).
- A fetched body is streamed and reading stops at `WSA_MAX_RESPONSE_BYTES`. It used to be
  read whole and trimmed afterwards, so the cap bounded what was parsed but not what was
  downloaded or held in memory.
- A 500 response carries `X-Request-ID` like every other response. Starlette renders
  unhandled errors outside the user middleware, so the context middleware never stamped it.
- Bind Serper to each request's caller so connected accounts can use the search backend.
- Close the Playwright driver after a failed browser launch.
- Hide resolved credential values and caller tokens in object representations.
- Correct keyring setup examples and session-persistence documentation.
