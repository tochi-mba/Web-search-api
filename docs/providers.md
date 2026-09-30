# Providers

54 providers: two with adapters of their own (Anthropic and Ollama) and 52 served by one
parameterised OpenAI-compatible adapter from a table.

## Adding an OpenAI-compatible provider

Most vendors speak OpenAI's wire format. Adding one is **a single row** in
`OPENAI_COMPATIBLE_SPECS`, in `app/services/llm/specs.py`, placed under the comment
for its kind:

```python
OPENAI_COMPATIBLE_SPECS: tuple[ProviderSpec, ...] = (
    # ... the existing rows, grouped by kind ...
    _spec("acme", "Acme AI", "https://api.acme.ai/v1"),
)
```

That is the entire change. The credential is not configured here and not held by
this service at all: `key` doubles as the service name the caller's credential is
stored under in keyring, resolved per request for whoever is asking. The
parametrised sweep in `tests/unit/test_openai_compatible.py` picks up the new row
automatically and fails if it is malformed.

### Spec fields

| Field | Purpose |
|---|---|
| `key` | Namespace in model ids (`acme:some-model`) |
| `label` | Human-readable name |
| `base_url` | API root, no trailing slash |
| `kind` | `frontier`, `aggregator`, `inference` (default) or `local`; the table aliases them `F`, `A`, `I`, `L` |
| `auth` | `bearer` (default), `header`, `query` or `none`. Decides how `scripts/provision_keyring.py` stores the key |
| `auth_header` | Header name when `auth="header"` |
| `models_path` | Defaults to `/models` |
| `chat_path` | Defaults to `/chat/completions` |
| `static_models` | Models to offer when there is no usable list endpoint |
| `docs_url` | Where the vendor documents its OpenAI-compatible endpoint, for whoever maintains the row |

There is no field naming an environment variable for a key. Keys live in keyring; the
provisioning script looks for `<KEY>_API_KEY` in its own environment only to copy it
there.

## Native adapters

Two APIs differ enough to warrant their own module under `app/services/llm/providers/`:

| Provider | Why | Discovery |
|---|---|---|
| Anthropic | Messages API through the `anthropic` SDK, top-level `system`, generation-specific thinking config, `stop_reason: "refusal"` on a 200 | `client.models.list()` |
| Ollama | Richer native metadata than the OpenAI shim; chat goes through the shim at `/v1` | `GET /api/tags` |

OpenAI and Gemini are rows in the table like everyone else, listed from `GET /models`.

## The catalogue

| Category | Providers |
|---|---|
| **Frontier** | OpenAI, Anthropic, Google Gemini, Mistral, DeepSeek, xAI Grok, Cohere, Moonshot Kimi, Z.AI GLM, Alibaba Qwen, Meta Llama, AI21 |
| **Fast inference** | Groq, Cerebras, Together, Fireworks, SambaNova, DeepInfra, Hyperbolic, Nebius, Novita, Baseten, Lambda, Featherless, Nscale, FriendliAI, NVIDIA NIM, Anyscale, Chutes, ModelScope, Inception, W&B, Clarifai, PublicAI, Galadriel |
| **Aggregators** | OpenRouter, Perplexity, Vercel AI Gateway, Helicone, GitHub Models, Poe, Nano-GPT, v0, Morph |
| **Local** | Ollama, LM Studio, vLLM, llama.cpp / llamafile, LocalAI, Xinference, Jan, text-generation-webui, KoboldCpp, Docker Model Runner |

Local runtimes need no credential — only a reachable base URL, overridable per
provider in `WSA_PROVIDER_BASE_URLS`, for example `{"ollama":"http://localhost:11434"}`.
An explicit `""` turns one off. Ollama alone also reads `OLLAMA_BASE_URL` when
`WSA_PROVIDER_BASE_URLS` names no `ollama` entry, and falls back to
`http://localhost:11434`.

## Availability

Providers are always constructed; whether they are *usable* is decided by
probing. `registry.probe()` calls each provider's list endpoint concurrently
with a short timeout and classifies the result:

| Status | Meaning |
|---|---|
| `available` | Responded; its models are offered |
| `unauthorized` | Reachable but rejected the credential |
| `unreachable` | Timed out, refused, or errored |
| `not_configured` | No credential or endpoint set |

Only `available` providers contribute models. Results are TTL-cached;
`GET /v1/models?refresh=true` re-probes.

## Model capabilities

Per-model differences are handled in `app/services/llm/capabilities.py`, never
inside an adapter. To teach the service about a new model family, add a rule to
`capability_rules.py` — ordered most-specific first — and a test in
`tests/unit/test_capabilities.py` pinning the exact wire body.

Capability flags: `supports_temperature`, `supports_top_p`, `supports_streaming`,
`supports_json_mode`, `supports_system_prompt`, `system_role`,
`max_tokens_param`, `reasoning`, `context_window`, `max_output_tokens`.

Reasoning styles: `none`, `effort` (OpenAI `reasoning_effort`),
`adaptive_thinking` (Anthropic 4.6+), `budget_tokens` (Anthropic pre-4.6),
`enable_thinking` (Qwen), `thinking_type` (GLM).

Unknown models fall back to conservative defaults — the minimal universally
accepted request — so a model released tomorrow still works.
