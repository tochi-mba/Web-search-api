# Providers

~60 providers across four native adapters and one parameterised adapter.

## Adding an OpenAI-compatible provider

Most vendors speak OpenAI's wire format. Adding one is **a single row** in
`app/services/llm/specs.py`:

```python
(_spec("acme", "Acme AI", "https://api.acme.ai/v1"),)
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
| `kind` | `frontier`, `aggregator`, `inference` or `local` |
| `api_key_env` | Environment variable holding the credential |
| `auth` | `bearer` (default), `header`, `query` or `none` |
| `auth_header` | Header name when `auth="header"` |
| `models_path` | Defaults to `/models` |
| `chat_path` | Defaults to `/chat/completions` |
| `base_url_env` | Endpoint override variable, for local runtimes |
| `static_models` | Models to offer when there is no usable list endpoint |

## Native adapters

Four APIs differ enough to warrant their own module:

| Provider | Why | Discovery |
|---|---|---|
| Anthropic | Messages API, top-level `system`, generation-specific thinking config, `stop_reason: "refusal"` on a 200 | `client.models.list()` |
| OpenAI | Official SDK | `client.models.list()` |
| Ollama | Richer native metadata than the OpenAI shim | `GET /api/tags` |
| Gemini | Native API, plus an OpenAI shim used by the table | `GET /models` |

## The catalogue

| Category | Providers |
|---|---|
| **Frontier** | OpenAI, Anthropic, Google Gemini, Mistral, DeepSeek, xAI Grok, Cohere, Moonshot Kimi, Z.AI GLM, Alibaba Qwen, Meta Llama, AI21 |
| **Fast inference** | Groq, Cerebras, Together, Fireworks, SambaNova, DeepInfra, Hyperbolic, Nebius, Novita, Baseten, Lambda, Featherless, Nscale, FriendliAI, NVIDIA NIM, Anyscale, Chutes, ModelScope, Inception, W&B, Clarifai, PublicAI, Galadriel |
| **Aggregators** | OpenRouter, Perplexity, Vercel AI Gateway, Helicone, GitHub Models, Poe, Nano-GPT, v0, Morph |
| **Local** | Ollama, LM Studio, vLLM, llama.cpp / llamafile, LocalAI, Xinference, Jan, text-generation-webui, KoboldCpp, Docker Model Runner |

Local runtimes need no credential — only a reachable base URL, overridable per
provider in `WSA_PROVIDER_BASE_URLS`, for example `{"ollama":"http://localhost:11434"}`.

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
