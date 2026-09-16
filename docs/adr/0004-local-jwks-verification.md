# 0004 — Tokens are verified locally against keyring's published keys

**Status:** Accepted

## Context

Credentials here belong to people, so almost everything below the HTTP layer needs to know
which person a request is for. Keyring holds the accounts, mints short-lived RS256 tokens,
and publishes the public keys to check them with.

There are two ways to connect a request to an account: ask keyring about the token on every
request, or verify keyring's signature locally. Keyring was built for the second — that is
what its signing-key document is *for* — and its own ADR-0008 already paid for that design
on behalf of the family. What was left to decide is what this side does with a token, and
what it agrees to live without by never asking.

## Decision

**web-search-api never calls keyring to check a token.** `app/services/keyring/tokens.py`
wraps `keyring_client`, the verifier every service in the family shares: a JWKS client that
fetches and caches keyring's keys, and a verifier that pins the issuer to
`WSA_KEYRING_ISSUER` and the audience to exactly `WSA_KEYRING_SERVICE_NAME`. The account is
the verified `sub` claim and comes from nowhere else — no request body, path or query
parameter names an account, so this service cannot ask for a credential it was not handed a
token for.

The rules for believing a token are the family's, in one shared library rather than a copy
here that could drift from it. What this repository adds is four local decisions:

- **Every refusal is the same refusal.** A bad signature, the wrong audience, the wrong
  issuer, an expired token and a missing claim all become one `AuthError` carrying one
  message. Which rule did the refusing goes to the log, where an operator reads it and a
  forger does not; a distinct message per failure is a free oracle for what the next
  forgery needs to look like.
- **Keyring being unreachable is a 503, never a 401.** `KeyringUnreachableError` becomes
  `ProviderUnavailableError`. A caller holding a perfectly good token must not be sent off
  to re-authenticate against a keyring that cannot answer either.
- **An import-linter contract keeps `keyring_client` and `jwt` inside
  `app.services.keyring`.** Everything this service asks of keyring, and every rule by which
  it believes an answer, is therefore in one package a reviewer can read in a sitting.
  `app.config` is deliberately outside that contract: it validates the service token and the
  audience using keyring's own rules rather than a second copy of them that could drift.
- **Presenting no token at all is legal.** Anonymous requests work against providers needing
  no credential and fail only when a secret is actually required. A token presented to a
  deployment with no keyring configured is a 503 saying so, rather than a 401 blaming the
  caller for a token that was never examined.

## Consequences

**A revoked session stays valid until its token expires.** Signing out of keyring ends the
session there and ends nothing here. The window is bounded by keyring's own token lifetime
and by nothing this service does. This is the price of local verification, it is keyring's
trade rather than ours, and the way to shorten it is to shorten keyring's lifetime — not to
add a revocation check, which would be a call to keyring on every request and is the whole
thing local verification exists to avoid.

**Cached keys are served through an outage for a bounded time** —
`WSA_JWKS_STALE_GRACE_SECONDS`, a day past the cache period by default — rather than
indefinitely, so a key keyring has withdrawn does not keep working for ever. Refusing good
tokens for the duration of a transient outage would be strictly worse; honouring a withdrawn
key permanently would be worse still.

**An unknown key id earns one refetch per `WSA_JWKS_MIN_REFETCH_SECONDS`.** A key id is read
from the token's unverified header before anything has been verified, because it is what
chooses the key that would do the verifying — which makes it the one value an
unauthenticated caller puts in front of the verifier. Without a floor, a stream of tokens
carrying invented key ids is one outbound request to keyring per inbound request, an
amplifier anyone who can reach this service can aim while holding no token and no account.
The cost is real: a genuine token signed with a freshly rotated key can land inside that
window and be refused exactly like a forgery. That is the rate limit working as designed,
and it is indistinguishable from outside by design.

**Two settings must agree with keyring or nothing works**, and both fail closed: the issuer
and the service name. An operator who renames this service in keyring must change
`WSA_KEYRING_SERVICE_NAME` to match, or every token is refused with the same message a
forgery gets.

Verification is tested against a real RS256 signer and a real signing-key document rather
than a stubbed verifier, because verification is a security control and a stubbed one proves
only that the stub works.

## What would change this

A revocation signal keyring could publish and this service could consume on the same
schedule as the keys — a short-lived deny list fetched alongside them — would shrink the
window without reintroducing a per-request call. That is a change to what keyring publishes
before it is a change here.

Nothing about the split itself. A per-request check would make every summary depend on a
second service being up, which is the failure the arrangement is built to avoid.
