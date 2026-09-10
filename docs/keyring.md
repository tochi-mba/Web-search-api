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

you ──► web-search-api   X-Keyring-User-Token: <that token>
        │
        ├─ verifies the token locally against keyring's published keys
        │  (RS256, audience checked, no round trip)
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
export KEYRING_SERVICE_TOKENS__WEB_SEARCH_API="$(openssl rand -hex 32)"
```

### 2. Point web-search-api at keyring

```bash
WSA_KEYRING_BASE_URL=http://127.0.0.1:8001
WSA_KEYRING_SERVICE_TOKEN=<the same value>
WSA_KEYRING_SERVICE_NAME=web-search-api
```

`WSA_KEYRING_SERVICE_NAME` must match the audience callers mint tokens for, and
the key under `KEYRING_SERVICE_TOKENS__`.

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

curl -s localhost:8000/v1/models -H "X-Keyring-User-Token: $USER_TOKEN" | jq
```

## Headers

| Header | Required | Meaning |
|---|---|---|
| `X-Keyring-User-Token` | for any credentialed provider | Who the request is for |
| `X-Keyring-Profile` | no | Which credential set; defaults to `WSA_KEYRING_DEFAULT_PROFILE` |

A request without a token is legal and still works against providers that need
no credential. It fails only when it actually needs a secret, with a `404`
naming the service to connect.

## Profiles

Profiles are named credential sets. `personal` and `work` can hold different
OpenAI keys, and the same account gets a different catalogue from each:

```bash
curl -s localhost:8000/v1/models \
  -H "X-Keyring-User-Token: $USER_TOKEN" \
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
carries the resolved headers rather than the token. Submitting without a usable
credential fails immediately rather than handing back a job id that cannot work.

API-key credentials never expire, which covers every LLM provider here. An
OAuth-backed credential nearing expiry is the one case that can still fail
mid-job.

## What this costs

Worth knowing before you depend on it:

- **Keyring down means no cloud provider works.** There is no environment
  fallback; that was the point.
- **Keyring v1 keeps accounts and sessions in memory.** A keyring restart
  invalidates every session, and everyone must log in again to mint tokens.
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
