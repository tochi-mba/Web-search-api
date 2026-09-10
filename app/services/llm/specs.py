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
    label: str
    base_url: str
    kind: ProviderKind = ProviderKind.INFERENCE
    api_key_env: str | None = None
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
        """Whether this provider needs a credential to work."""
        return self.auth is not AuthStyle.NONE and self.api_key_env is not None


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
    _spec("openai", "OpenAI", "https://api.openai.com/v1", kind=F, api_key_env="OPENAI_API_KEY"),
    _spec(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        kind=F,
        api_key_env="GEMINI_API_KEY",
        docs_url="https://ai.google.dev/gemini-api/docs/openai",
    ),
    _spec("mistral", "Mistral", "https://api.mistral.ai/v1", kind=F, api_key_env="MISTRAL_API_KEY"),
    _spec(
        "deepseek", "DeepSeek", "https://api.deepseek.com", kind=F, api_key_env="DEEPSEEK_API_KEY"
    ),
    _spec("xai", "xAI Grok", "https://api.x.ai/v1", kind=F, api_key_env="XAI_API_KEY"),
    _spec(
        "cohere",
        "Cohere",
        "https://api.cohere.ai/compatibility/v1",
        kind=F,
        api_key_env="COHERE_API_KEY",
    ),
    _spec(
        "moonshot",
        "Moonshot Kimi",
        "https://api.moonshot.ai/v1",
        kind=F,
        api_key_env="MOONSHOT_API_KEY",
    ),
    _spec("zai", "Z.AI GLM", "https://api.z.ai/api/paas/v4", kind=F, api_key_env="ZAI_API_KEY"),
    _spec(
        "dashscope",
        "Alibaba DashScope (Qwen)",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        kind=F,
        api_key_env="DASHSCOPE_API_KEY",
    ),
    _spec(
        "meta_llama",
        "Meta Llama API",
        "https://api.llama.com/compat/v1",
        kind=F,
        api_key_env="LLAMA_API_KEY",
    ),
    _spec(
        "ai21", "AI21 Labs", "https://api.ai21.com/studio/v1", kind=F, api_key_env="AI21_API_KEY"
    ),
    # -------------------------------------------------------- fast inference
    _spec("groq", "Groq", "https://api.groq.com/openai/v1", api_key_env="GROQ_API_KEY"),
    _spec("cerebras", "Cerebras", "https://api.cerebras.ai/v1", api_key_env="CEREBRAS_API_KEY"),
    _spec("together", "Together AI", "https://api.together.xyz/v1", api_key_env="TOGETHER_API_KEY"),
    _spec(
        "fireworks",
        "Fireworks AI",
        "https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY",
    ),
    _spec("sambanova", "SambaNova", "https://api.sambanova.ai/v1", api_key_env="SAMBANOVA_API_KEY"),
    _spec(
        "deepinfra",
        "DeepInfra",
        "https://api.deepinfra.com/v1/openai",
        api_key_env="DEEPINFRA_API_KEY",
    ),
    _spec(
        "hyperbolic",
        "Hyperbolic",
        "https://api.hyperbolic.xyz/v1",
        api_key_env="HYPERBOLIC_API_KEY",
    ),
    _spec(
        "nebius",
        "Nebius AI Studio",
        "https://api.studio.nebius.ai/v1",
        api_key_env="NEBIUS_API_KEY",
    ),
    _spec("novita", "Novita AI", "https://api.novita.ai/openai", api_key_env="NOVITA_API_KEY"),
    _spec("baseten", "Baseten", "https://inference.baseten.co/v1", api_key_env="BASETEN_API_KEY"),
    _spec("lambda_ai", "Lambda", "https://api.lambda.ai/v1", api_key_env="LAMBDA_API_KEY"),
    _spec(
        "featherless",
        "Featherless AI",
        "https://api.featherless.ai/v1",
        api_key_env="FEATHERLESS_API_KEY",
    ),
    _spec("nscale", "Nscale", "https://inference.api.nscale.com/v1", api_key_env="NSCALE_API_KEY"),
    _spec(
        "friendli",
        "FriendliAI",
        "https://api.friendli.ai/serverless/v1",
        api_key_env="FRIENDLI_TOKEN",
    ),
    _spec(
        "nvidia", "NVIDIA NIM", "https://integrate.api.nvidia.com/v1", api_key_env="NVIDIA_API_KEY"
    ),
    _spec(
        "anyscale",
        "Anyscale",
        "https://api.endpoints.anyscale.com/v1",
        api_key_env="ANYSCALE_API_KEY",
    ),
    _spec("chutes", "Chutes", "https://llm.chutes.ai/v1", api_key_env="CHUTES_API_KEY"),
    _spec(
        "modelscope",
        "ModelScope",
        "https://api-inference.modelscope.cn/v1",
        api_key_env="MODELSCOPE_API_KEY",
    ),
    _spec(
        "inception",
        "Inception Labs",
        "https://api.inceptionlabs.ai/v1",
        api_key_env="INCEPTION_API_KEY",
    ),
    _spec(
        "wandb", "W&B Inference", "https://api.inference.wandb.ai/v1", api_key_env="WANDB_API_KEY"
    ),
    _spec(
        "clarifai",
        "Clarifai",
        "https://api.clarifai.com/v2/ext/openai/v1",
        api_key_env="CLARIFAI_PAT",
    ),
    _spec("publicai", "PublicAI", "https://api.publicai.co/v1", api_key_env="PUBLICAI_API_KEY"),
    _spec("galadriel", "Galadriel", "https://api.galadriel.ai/v1", api_key_env="GALADRIEL_API_KEY"),
    # -------------------------------------------------------------- aggregators
    _spec(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        kind=A,
        api_key_env="OPENROUTER_API_KEY",
        docs_url="https://openrouter.ai/docs/quickstart",
    ),
    _spec(
        "perplexity",
        "Perplexity",
        "https://api.perplexity.ai",
        kind=A,
        api_key_env="PERPLEXITY_API_KEY",
        static_models=("sonar", "sonar-pro", "sonar-reasoning", "sonar-reasoning-pro"),
    ),
    _spec(
        "vercel_gateway",
        "Vercel AI Gateway",
        "https://ai-gateway.vercel.sh/v1",
        kind=A,
        api_key_env="VERCEL_AI_GATEWAY_KEY",
    ),
    _spec(
        "helicone",
        "Helicone Gateway",
        "https://ai-gateway.helicone.ai",
        kind=A,
        api_key_env="HELICONE_API_KEY",
    ),
    _spec(
        "github_models",
        "GitHub Models",
        "https://models.github.ai/inference",
        kind=A,
        api_key_env="GITHUB_TOKEN",
    ),
    _spec("poe", "Poe", "https://api.poe.com/v1", kind=A, api_key_env="POE_API_KEY"),
    _spec(
        "nano_gpt", "Nano-GPT", "https://nano-gpt.com/api/v1", kind=A, api_key_env="NANOGPT_API_KEY"
    ),
    _spec("v0", "Vercel v0", "https://api.v0.dev/v1", kind=A, api_key_env="V0_API_KEY"),
    _spec("morph", "Morph", "https://api.morphllm.com/v1", kind=A, api_key_env="MORPH_API_KEY"),
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
