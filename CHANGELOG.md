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

- CI inherits `FAMILY_GITHUB_TOKEN`; image builds accept a BuildKit `github_token`
  secret so tagged client packages can be fetched from private family repositories.
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

### Fixed

- Bind Serper to each request's caller so connected accounts can use the search backend.
- Close the Playwright driver after a failed browser launch.
- Hide resolved credential values and caller tokens in object representations.
- Correct keyring setup examples and session-persistence documentation.
