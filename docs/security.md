# Security

## Threat model

This service fetches URLs chosen by whoever calls it. Without controls that is
a server-side request forgery primitive: a caller can point it at cloud
metadata endpoints to steal instance credentials, or sweep an internal network
using the service as a proxy.

## Controls

### URL validation (`app/services/urlsafety.py`)

Every caller-supplied URL must be:

- `http` or `https` — no `file:`, `ftp:`, `gopher:`, `javascript:`
- a parseable URL with a host component
- **not** a cloud metadata hostname (`metadata.google.internal`, `metadata.goog`,
  `instance-data`, `metadata`) — blocked by name as well as by address, since an
  address check alone can be defeated by DNS games
- **not** `localhost` and friends
- resolving **only** to globally routable addresses

Blocked address ranges: loopback, private (RFC 1918), link-local (including
`169.254.169.254`), reserved, unspecified and multicast, in both IPv4 and IPv6.
If *any* address a hostname resolves to is blocked, the whole host is blocked.

### Redirect re-validation

**This is the control most implementations miss.** Validating only the caller's
URL is worthless if that URL is allowed to redirect to `127.0.0.1`.

Redirects are therefore followed **manually** rather than by httpx, and every
hop goes back through the same validator. `tests/unit/test_http_fetcher.py`
covers redirect-to-private-address and redirect-to-metadata explicitly.

### Escape hatch

`WSA_ALLOW_PRIVATE_NETWORKS=true` permits private addresses for deployments
crawling their own infrastructure. **Cloud metadata hosts remain blocked even
then**, because there is no legitimate reason for this service to read them.

### Other limits

| Control | Setting |
|---|---|
| Response size cap | `WSA_MAX_RESPONSE_BYTES` (5 MB) |
| Redirect budget | `WSA_MAX_REDIRECTS` (5) |
| Request timeout | `WSA_REQUEST_TIMEOUT_SECONDS` (20s) |
| Content-type allowlist | text, xhtml, xml, json only |
| Global concurrency | `WSA_MAX_CONCURRENCY` (8) |
| Per-host concurrency | `WSA_MAX_CONCURRENCY_PER_HOST` (2) |
| robots.txt | `WSA_RESPECT_ROBOTS` (true) |

## Prompt injection

Scraped pages are untrusted text that reaches a language model. Full mitigation
is not possible today, but the prompt makes the boundary explicit: content is
fenced by `--- BEGIN SOURCE TEXT ---` markers and the model is told that
anything inside is data rather than instructions. Caller `additional_notes` are
placed with the instructions instead of the content and framed as guidance
about focus, not a source of facts.

Treat summaries of untrusted pages as untrusted output. Do not feed them into
anything that executes actions without review.

## Authentication

Off by default. Set `WSA_API_KEYS` to a comma-separated list to require a key
via `X-API-Key` or `Authorization: Bearer`. Health endpoints stay public so
orchestrator probes keep working.

Keys are compared against a configured list. For anything beyond a trusted
network, put a real gateway in front.

## Credentials

Provider API keys are **not** held by this service. They live in keyring and are
resolved per request for the person the request is being made for — see
[keyring.md](keyring.md).

What that buys:

- No third-party secret sits in this service's environment, image or config.
- One person's key cannot be used to serve another person's request. The account
  is taken from a signed token, and there is no parameter by which this service
  could name an account.
- Keyring returns *what to attach* — a header — never the stored secret, and
  never a refresh token.

What it does not buy: web-search-api necessarily handles resolved credentials in
memory to attach them to outgoing calls. It never stores or logs them, and
`GET /v1/models` reports *whether* a credential worked, never the credential.

User tokens are verified locally against keyring's published JWKS with the
algorithm pinned to RS256 — an `alg: none` downgrade, a forged signature, an
expired token and a token minted for a different service are all refused, and
every failure returns the same message so a caller holding a forged token learns
nothing from which check failed.
