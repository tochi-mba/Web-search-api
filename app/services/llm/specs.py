"""The provider registry table.

Almost the entire market speaks OpenAI's wire format, so nearly every vendor
here is served by one tested adapter parameterised by a row in this table.
Adding a provider is a row, not a class - which is also what keeps a 100%
coverage gate tractable across this many vendors.

Base URLs were verified against provider documentation and LiteLLM's
``openai_compatible_endpoints`` registry in September 2026.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AuthStyle(StrEnum):
    """How credentials are presented to a provider."""

    BEARER = "bearer"
    """``Authorization: Bearer <key>``, the OpenAI convention."""

    HEADER = "header"
    """A vendor-specific header named by :attr:`ProviderSpec.auth_header`."""

    QUERY = "query"
    """An API key in the query string."""

    NONE = "none"
    """No credentials, typical of local runtimes."""


class ProviderKind(StrEnum):
    """Broad category, used for documentation and filtering."""

    FRONTIER = "frontier"
    """First-party APIs from frontier labs."""

    AGGREGATOR = "aggregator"
    """Gateways and routers fronting many upstream models."""

    INFERENCE = "inference"
    """Hosted inference vendors serving open-weight models."""

    LOCAL = "local"
    """Runtimes on the caller's own machine or network."""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Everything needed to talk to one OpenAI-compatible provider."""

    key: str
    """Also the service name this provider's credential is stored under in keyring."""

    label: str
    base_url: str
    kind: ProviderKind = ProviderKind.INFERENCE
    auth: AuthStyle = AuthStyle.BEARER
    auth_header: str | None = None
    models_path: str = "/models"
    chat_path: str = "/chat/completions"
    base_url_env: str | None = None
    #: Models to offer when the provider exposes no usable list endpoint.
    static_models: tuple[str, ...] = field(default_factory=tuple)
    docs_url: str = ""

    @property
    def requires_key(self) -> bool:
        """Whether this provider needs a credential to work.

        False for local runtimes, which have no secret for keyring to hold and
        so are usable without a caller identity at all.
        """
        return self.auth is not AuthStyle.NONE

    @property
    def default_header(self) -> str:
        """Header this provider expects its key on, for provisioning keyring."""
        if self.auth is AuthStyle.HEADER and self.auth_header:
            return self.auth_header
        return "Authorization"

    @property
    def default_template(self) -> str:
        """Template keyring should store the key under, for provisioning."""
        if self.auth is AuthStyle.BEARER:
            return "Bearer {value}"
        return "{value}"


def _spec(key: str, label: str, base_url: str, **kwargs: object) -> ProviderSpec:
    """Build a provider spec with the common defaults."""
    return ProviderSpec(key=key, label=label, base_url=base_url, **kwargs)  # type: ignore[arg-type]


F = ProviderKind.FRONTIER
A = ProviderKind.AGGREGATOR
I = ProviderKind.INFERENCE  # noqa: E741 - a one-letter alias keeps the table readable
L = ProviderKind.LOCAL


#: Every OpenAI-compatible provider this service knows about.
OPENAI_COMPATIBLE_SPECS: tuple[ProviderSpec, ...] = (
    # ------------------------------------------------------------- frontier labs
    _spec("openai", "OpenAI", "https://api.openai.com/v1", kind=F),
    _spec(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        kind=F,
        docs_url="https://ai.google.dev/gemini-api/docs/openai",
    ),
    _spec("mistral", "Mistral", "https://api.mistral.ai/v1", kind=F),
    _spec("deepseek", "DeepSeek", "https://api.deepseek.com", kind=F),
    _spec("xai", "xAI Grok", "https://api.x.ai/v1", kind=F),
    _spec(
        "cohere",
        "Cohere",
        "https://api.cohere.ai/compatibility/v1",
        kind=F,
    ),
    _spec(
        "moonshot",
        "Moonshot Kimi",
        "https://api.moonshot.ai/v1",
        kind=F,
    ),
    _spec("zai", "Z.AI GLM", "https://api.z.ai/api/paas/v4", kind=F),
    _spec(
        "dashscope",
        "Alibaba DashScope (Qwen)",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        kind=F,
    ),
    _spec(
        "meta_llama",
        "Meta Llama API",
        "https://api.llama.com/compat/v1",
        kind=F,
    ),
    _spec("ai21", "AI21 Labs", "https://api.ai21.com/studio/v1", kind=F),
    # -------------------------------------------------------- fast inference
    _spec("groq", "Groq", "https://api.groq.com/openai/v1"),
    _spec("cerebras", "Cerebras", "https://api.cerebras.ai/v1"),
    _spec("together", "Together AI", "https://api.together.xyz/v1"),
    _spec(
        "fireworks",
        "Fireworks AI",
        "https://api.fireworks.ai/inference/v1",
    ),
    _spec("sambanova", "SambaNova", "https://api.sambanova.ai/v1"),
    _spec(
        "deepinfra",
        "DeepInfra",
        "https://api.deepinfra.com/v1/openai",
    ),
    _spec(
        "hyperbolic",
        "Hyperbolic",
        "https://api.hyperbolic.xyz/v1",
    ),
    _spec(
        "nebius",
        "Nebius AI Studio",
        "https://api.studio.nebius.ai/v1",
    ),
    _spec("novita", "Novita AI", "https://api.novita.ai/openai"),
    _spec("baseten", "Baseten", "https://inference.baseten.co/v1"),
    _spec("lambda_ai", "Lambda", "https://api.lambda.ai/v1"),
    _spec(
        "featherless",
        "Featherless AI",
        "https://api.featherless.ai/v1",
    ),
    _spec("nscale", "Nscale", "https://inference.api.nscale.com/v1"),
    _spec(
        "friendli",
        "FriendliAI",
        "https://api.friendli.ai/serverless/v1",
    ),
    _spec("nvidia", "NVIDIA NIM", "https://integrate.api.nvidia.com/v1"),
    _spec(
        "anyscale",
        "Anyscale",
        "https://api.endpoints.anyscale.com/v1",
    ),
    _spec("chutes", "Chutes", "https://llm.chutes.ai/v1"),
    _spec(
        "modelscope",
        "ModelScope",
        "https://api-inference.modelscope.cn/v1",
    ),
    _spec(
        "inception",
        "Inception Labs",
        "https://api.inceptionlabs.ai/v1",
    ),
    _spec("wandb", "W&B Inference", "https://api.inference.wandb.ai/v1"),
    _spec(
        "clarifai",
        "Clarifai",
        "https://api.clarifai.com/v2/ext/openai/v1",
    ),
    _spec("publicai", "PublicAI", "https://api.publicai.co/v1"),
    _spec("galadriel", "Galadriel", "https://api.galadriel.ai/v1"),
    # -------------------------------------------------------------- aggregators
    _spec(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        kind=A,
        docs_url="https://openrouter.ai/docs/quickstart",
    ),
    _spec(
        "perplexity",
        "Perplexity",
        "https://api.perplexity.ai",
        kind=A,
        static_models=("sonar", "sonar-pro", "sonar-reasoning", "sonar-reasoning-pro"),
    ),
    _spec(
        "vercel_gateway",
        "Vercel AI Gateway",
        "https://ai-gateway.vercel.sh/v1",
        kind=A,
    ),
    _spec(
        "helicone",
        "Helicone Gateway",
        "https://ai-gateway.helicone.ai",
        kind=A,
    ),
    _spec(
        "github_models",
        "GitHub Models",
        "https://models.github.ai/inference",
        kind=A,
    ),
    _spec("poe", "Poe", "https://api.poe.com/v1", kind=A),
    _spec("nano_gpt", "Nano-GPT", "https://nano-gpt.com/api/v1", kind=A),
    _spec("v0", "Vercel v0", "https://api.v0.dev/v1", kind=A),
    _spec("morph", "Morph", "https://api.morphllm.com/v1", kind=A),
    # ------------------------------------------------------------ local runtimes
    _spec(
        "lmstudio",
        "LM Studio",
        "http://localhost:1234/v1",
        kind=L,
        auth=AuthStyle.NONE,
        base_url_env="LMSTUDIO_BASE_URL",
    ),
    _spec(
        "vllm",
        "vLLM",
        "http://localhost:8000/v1",
        kind=L,
        auth=AuthStyle.NONE,
        base_url_env="VLLM_BASE_URL",
    ),
    _spec(
        "llamacpp",
        "llama.cpp / llamafile",
        "http://localhost:8080/v1",
        kind=L,
        auth=AuthStyle.NONE,
        base_url_env="LLAMACPP_BASE_URL",
    ),
    _spec(
        "localai",
        "LocalAI",
        "http://localhost:8080/v1",
        kind=L,
        auth=AuthStyle.NONE,
        base_url_env="LOCALAI_BASE_URL",
    ),
    _spec(
        "xinference",
        "Xinference",
        "http://localhost:9997/v1",
        kind=L,
        auth=AuthStyle.NONE,
        base_url_env="XINFERENCE_BASE_URL",
    ),
    _spec(
        "jan",
        "Jan",
        "http://localhost:1337/v1",
        kind=L,
        auth=AuthStyle.NONE,
        base_url_env="JAN_BASE_URL",
    ),
    _spec(
        "textgen_webui",
        "text-generation-webui",
        "http://localhost:5000/v1",
        kind=L,
        auth=AuthStyle.NONE,
        base_url_env="TEXTGEN_BASE_URL",
    ),
    _spec(
        "koboldcpp",
        "KoboldCpp",
        "http://localhost:5001/v1",
        kind=L,
        auth=AuthStyle.NONE,
        base_url_env="KOBOLDCPP_BASE_URL",
    ),
    _spec(
        "docker_model_runner",
        "Docker Model Runner",
        "http://localhost:12434/engines/v1",
        kind=L,
        auth=AuthStyle.NONE,
        base_url_env="DOCKER_MODEL_RUNNER_BASE_URL",
    ),
)

#: Fast lookup by provider key.
SPECS_BY_KEY: dict[str, ProviderSpec] = {spec.key: spec for spec in OPENAI_COMPATIBLE_SPECS}
