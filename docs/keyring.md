# Credentials and keyring

web-search-api stores **no third-party secrets**. Every provider key lives in
[keyring](https://github.com/tochi-mba/keyring-api), belongs to a person, and is
resolved at the moment it is needed.

That has a consequence worth stating up front: **answers here are per caller.**
`GET /v1/models` shows the providers *you* have connected, not what the
deployment has configured, because there is no such thing as a deployment-wide
credential any more.

## How a request works

```
you ──► keyring   POST /v1/auth/service-token {"audience": "web-search-api"}
                  └─► a short-lived signed token

you ──► web-search-api   Authorization: Bearer <that token>
        │
        ├─ verifies the token locally against keyring's published keys
        │  (RS256, issuer and audience pinned, signing keys cached)
        │
        └─► keyring   GET /v1/internal/credentials/{profile}/{service}
                      Authorization: Bearer <web-search-api's service token>
                      X-Keyring-User-Token: <your token>
                      └─► {"headers": {...}, "query_params": {...}}
                          attached to the outgoing provider call
```

Two credentials are required on that inner call: this service's own token,
proving which service is asking, and your token, proving you authorised it.
**The account is taken from your token** — there is no parameter by which a
service can name an account, so web-search-api cannot request a credential it
was not given a token for.

## Setting it up

### 1. Tell keyring about this service

```bash
SERVICE_TOKEN=$(openssl rand -hex 32)
export KEYRING_SERVICE_TOKENS="$(jq -nc --arg token "$SERVICE_TOKEN" '{"web-search-api": $token}')"
```

### 2. Point web-search-api at keyring

```bash
WSA_KEYRING_BASE_URL=http://127.0.0.1:8001
WSA_KEYRING_SERVICE_TOKEN=<the same value>
WSA_KEYRING_SERVICE_NAME=web-search-api
```

`WSA_KEYRING_SERVICE_NAME` must match the audience callers mint tokens for, and
the key in keyring's `KEYRING_SERVICE_TOKENS` JSON mapping.
Set `WSA_KEYRING_ISSUER` to keyring's `KEYRING_ISSUER` (locally,
`http://127.0.0.1:8001`).

### 3. Store your provider keys

**Use the script.** Keyring's api-key connections default to
`Authorization: Bearer {value}`, but Anthropic wants `x-api-key` and some
vendors want the key in the query string. Store one with the wrong shape and the
provider returns 401 with no hint as to why. web-search-api knows the right
answer for every provider it supports:

```bash
export KEYRING_SESSION_TOKEN=...          # POST /v1/auth/login
export ANTHROPIC_API_KEY=... GROQ_API_KEY=...
uv run python scripts/provision_keyring.py --profile personal
```

`--print` shows the curl commands without running them, `--check` reports what
is already connected, and `--only anthropic groq` limits the run.

### 4. Call the API

```bash
USER_TOKEN=$(curl -sX POST http://127.0.0.1:8001/v1/auth/service-token \
  -H "Authorization: Bearer $KEYRING_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"audience":"web-search-api"}' | jq -r .token)

curl -s localhost:8006/v1/models -H "Authorization: Bearer $USER_TOKEN" | jq
```

## Headers

| Header | Required | Meaning |
|---|---|---|
| `Authorization: Bearer <token>` | for any credentialed provider | Who the request is for |
| `X-Keyring-User-Token` | legacy alternative for one release | Deprecated; conflicting identity headers are rejected |
| `X-API-Key` | when the deployment configures API keys | Separate front-door gate; a Bearer token does not replace it |
| `X-Keyring-Profile` | no | Which credential set; defaults to this person's `common.default_profile`, or `WSA_KEYRING_DEFAULT_PROFILE` when settings-api is off |

A request without a token is legal and still works against providers that need
no credential. It fails only when it actually needs a secret, with a `404`
naming the service to connect.

## Profiles

Profiles are named credential sets. `personal` and `work` can hold different
OpenAI keys, and the same account gets a different catalogue from each:

```bash
curl -s localhost:8006/v1/models \
  -H "Authorization: Bearer $USER_TOKEN" \
  -H "X-Keyring-Profile: work"
```

## Local models need none of this

Ollama, LM Studio, vLLM, llama.cpp, LocalAI, Xinference, Jan, KoboldCpp,
text-generation-webui and Docker Model Runner hold no secret, so keyring is
never consulted for them. **A deployment running only local models needs no
keyring at all** — leave `WSA_KEYRING_BASE_URL` empty and everything works.

## Background jobs

A user token lives minutes; a twenty-URL scrape can outlast one. So a background
job resolves its credential **at submit time**, while your token is fresh, and
carries the resolved LLM headers. Serper resolves its own credential per query;
a long-running search batch still needs its caller token to remain valid. Submitting without a usable
credential fails immediately rather than handing back a job id that cannot work.

API-key credentials never expire, which covers every LLM provider here. An
OAuth-backed credential nearing expiry is the one case that can still fail
mid-job.

## What this costs

Worth knowing before you depend on it:

- **Keyring down means no cloud provider works.** There is no environment
  fallback; that was the point.
- **Keyring persists accounts and sessions in SQLite.** Restarting it preserves sessions;
  signed service tokens remain usable until their own expiry.
- **Building a catalogue costs one keyring call per provider.** Keyring's
  internal API has no "list this user's connections" endpoint, so availability
  is discovered by asking. Answers are cached per `(account, profile)` — keyed
  on the verified account id rather than the token, so rotating tokens does not
  throw the cache away.
- **web-search-api can see credentials in flight.** It attaches them to outgoing
  requests, so it necessarily handles them. It never stores or logs them, and it
  never sees a refresh token — keyring returns what to attach, not what it holds.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401 auth_error` | Token expired, forged, or minted for a different audience. Mint a new one with `"audience": "web-search-api"`. |
| `503` naming keyring | Keyring is unreachable, or a token was presented to a deployment with no keyring configured. |
| Provider shows `not_configured` with a token present | That account has not connected it. `scripts/provision_keyring.py --check`. |
| Provider connected but returns 401 upstream | The key is stored with the wrong header. Re-run the provisioning script, which sets it correctly. |
| `404` on a model you have a key for | Wrong profile. Check `X-Keyring-Profile`. |

## Shared verification and configuration changes

Token verification uses `keyring-client` from the sibling `Keyring-api/clients/python`
checkout. Install the family together while the client is unreleased. Keys are fetched
lazily, refreshes are rate limited, and cached keys survive a bounded outage. A failed
verification exposes one refusal message; HTTP exceptions and JWT diagnostics stay out
of responses.

`WSA_ENABLED_PROVIDERS` and `WSA_MAX_CONCURRENCY_PER_HOST` were never implemented and
are now rejected at startup. Provider URLs belong in `WSA_PROVIDER_BASE_URLS` (a JSON
mapping), including local runtimes. `.env.example` is tested through the real settings
loader. Serper is bound to each request's verified caller before availability is checked.
