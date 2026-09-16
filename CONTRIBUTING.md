# Contributing

Start with [AGENTS.md](AGENTS.md) — it is the operating manual for this repository and it
is normative. This file is the short version.

## Setup

```bash
make install             # venv and every dependency, from the lockfile
uv run pre-commit install
make check               # confirm a clean checkout is green before you change anything
```

The default suite needs no credentials and no network. The browser tests need Chromium:

```bash
uv run playwright install chromium
```

## The loop

1. **Write the failing test first.** Run it and confirm it fails for the reason you expect.
2. Write the smallest code that makes it pass.
3. Refactor with it green.
4. `make check` — lint, strict types, the layering contracts, and the tests at 100%
   branch coverage.

All four must pass before you commit.

## Fetching the open web safely

This service takes URLs from callers and fetches them, which makes it the one place in the
family where server-side request forgery is a live risk. `app/services/urlsafety.py` owns
that: a URL is refused unless it resolves to a globally routable address, and the check
happens after resolution, not before. Anything that adds a fetch path goes through it, and
a change to it needs tests for the private, loopback, link-local and redirect cases.

Playwright stays behind the browser adapter. An import contract enforces it, so a new
module that reaches for the driver directly fails `make imports` rather than review.

## Providers

A provider is an adapter with a recipe, not a special case in the router. Adding one means
a module under `app/services/`, an entry in the registry, and tests that cover both the
answer and the refusal. A provider that needs a credential resolves it per caller from
keyring — never from this service's own configuration.

## Commits

Conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`). The
subject says what changed; the body says **why**, and flags anything surprising.

Never commit an API key, a real token, or a `.env`.
