# Architecture decision records

One file per decision that a future reader would otherwise re-litigate. Each says what was
decided, what it cost, and what would change the answer.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-ssrf-is-the-threat-model.md) | SSRF is the threat model, and the check is on resolved addresses | Accepted |
| [0002](0002-providers-are-adapters-behind-a-registry.md) | Providers are adapters behind a registry, holding nobody's credential | Accepted |
| [0003](0003-playwright-behind-one-adapter.md) | Playwright stays behind one adapter, enforced in CI | Accepted |
| [0004](0004-local-jwks-verification.md) | Tokens are verified locally against keyring's published keys | Accepted |

Family-wide decisions — the shared client libraries, `Authorization: Bearer` as the
canonical identity header, the port assignments — live in the meta repository's `docs/adr/`
and are linked from here rather than restated.

## Writing one

Copy the shape of an existing record: context, decision, consequences, and what would
change the answer. Number it in sequence. If a decision only affects one module, a
docstring is the better home; an ADR is for the ones somebody will otherwise undo by
accident.
