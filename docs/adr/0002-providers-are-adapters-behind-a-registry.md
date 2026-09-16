# 0002 — Providers are adapters behind a registry, holding nobody's credential

**Status:** Accepted

## Context

This service summarises with whichever model the caller asks for, across roughly sixty
providers. Two questions had to be answered together, because the obvious answer to each
makes the other worse.

**How many adapters?** One class per vendor is sixty classes, sixty sets of tests, and a
100% coverage gate that nobody can hold up.

**Where do credentials live?** The obvious version is an API key per provider in this
service's environment — `WSA_OPENAI_API_KEY`, `WSA_GROQ_API_KEY`, and so on down the list.
That is what this service did before keyring existed. It means every caller shares one
identity, one person's key pays for another person's request, and sixty third-party secrets
sit in one process's environment.

## Decision

**One protocol, few adapters, and no credential held anywhere.**

`LLMProvider` is the whole seam: a `name`, a `requires_credential` flag, `is_configured()`,
`list_models(auth)` and `chat(request, auth)`. Nothing above this layer knows which vendor
is serving a request.

Almost the entire market speaks OpenAI's wire format, so one parameterised adapter serves
about fifty vendors from a row in `app/services/llm/specs.py` — a key, a label, a base URL,
and how the credential is presented. Adding a vendor is that row. Anthropic and Ollama have
their own modules because their APIs genuinely differ: Anthropic's Messages API puts the
system prompt at the top level and configures thinking differently per model generation,
and Ollama's native `/api/tags` reports richer metadata than its OpenAI shim.

**Credentials arrive per call.** A `ResolvedAuth` — the headers and query parameters
keyring resolved for one person — is an argument to every `list_models` and every `chat`.
No provider holds a key, reads one from the environment, or caches one. One provider object
therefore serves every caller and holds nobody's secret.

Three details follow from that, each deliberate:

- **Anthropic's SDK wants its key at construction, so a client is built per call** from
  that caller's resolved headers. The headers are passed through as default headers rather
  than unpacked into `api_key`, because keyring decides which header its key belongs on and
  second-guessing that here breaks the moment somebody stores a credential this code did
  not anticipate.
- **`is_configured()` answers about endpoints and never about credentials.** Whether a
  provider is *usable* is a different question, answered per caller by probing it.
- **Every provider is constructed even when it is disabled.** Filtering happens per caller
  at catalogue time. Disabling at process start would make a per-person "never send my
  queries there" impossible without a restart.

The registry probes providers concurrently, classifies each outcome into a status, and
caches the result per `(account id, profile, disabled-provider set)` — keyed on the
**verified** account id, never on anything the caller supplies, because serving one
person's catalogue to another is exactly the failure this design exists to prevent.

## Consequences

`GET /v1/models` lists only what is reachable right now, **for you**. A provider you have
not connected, or whose credential has stopped working, is reported with a status and
contributes no models, so anything listed can actually be used. A typo'd key surfaces as an
obvious `unauthorized` in the catalogue instead of a 500 halfway through somebody's
request.

The catalogue cannot be computed once at startup, because it is a different answer for
different people.

**Building one costs a keyring call per credentialed provider.** Keyring's internal API has
no "list this account's connections" endpoint, so availability is discovered by asking. The
short-circuit for an unconnected provider is what keeps a sixty-provider sweep cheap: it
never touches the provider at all, and for most people most providers are unconnected.

**Keyring being down means no credentialed provider works.** There is no environment
fallback. That was the point, and the local runtimes keep working throughout.

The per-caller cache is bounded, dropping the oldest entry once the limit is reached. Crude,
and better than an unbounded map of strangers' catalogues.

This service still handles credentials in flight — it must, to attach them to an outgoing
call. It never stores or logs them, and keyring returns *what to attach* rather than what it
holds, so a refresh token never arrives here at all.

Adding a vendor is one row and no new test: the parameterised sweep runs every row against a
mock OpenAI-compatible server, which is what makes a 100% gate tractable across this many
vendors and what fails CI on a malformed row.

## What would change this

A keyring endpoint that listed an account's connections would replace the probe sweep with a
single call, and would be worth taking.

A vendor whose API cannot be expressed as list-models-and-chat needs its own module rather
than a row — which is already true of two of them, and is the reason the protocol rather
than the table is the real seam.
