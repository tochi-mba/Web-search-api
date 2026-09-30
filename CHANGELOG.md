# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Changed

- **Breaking:** the floor is now **Python 3.12** (CI runs 3.12 and 3.13).
  `.python-version`, `requires-python`, ruff's `target-version`, mypy's `python_version`,
  the Docker base image and the pre-commit interpreter all moved together, and `uv.lock`
  was regenerated. The family-wide reason is in the meta-repo's
  [ADR-0008](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0008-python-3-12-floor.md):
  `weftai`, which the assistant hub depends on, requires 3.12 and uses PEP 695 type
  parameters that do not parse on 3.11. Generics here moved to PEP 695 syntax with it.
### Added

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

### Fixed

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
