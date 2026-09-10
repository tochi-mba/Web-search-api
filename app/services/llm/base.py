"""The provider-agnostic LLM seam.

Every provider - Anthropic, OpenAI, Ollama, and the whole OpenAI-compatible
fleet - is reduced to this interface, so nothing above this layer knows or cares
which vendor is serving a request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.services.keyring.client import ResolvedAuth


class ProviderStatus(StrEnum):
    """Whether a provider can currently serve requests."""

    AVAILABLE = "available"
    UNAUTHORIZED = "unauthorized"
    UNREACHABLE = "unreachable"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One model a provider is currently offering."""

    id: str
    """Namespaced identifier, e.g. ``anthropic:claude-opus-5``."""

    provider: str
    model: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None

    @staticmethod
    def build(provider: str, model: str, **kwargs: object) -> ModelInfo:
        """Construct a :class:`ModelInfo` with a correctly namespaced id."""
        return ModelInfo(id=f"{provider}:{model}", provider=provider, model=model, **kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """The result of probing one provider."""

    name: str
    status: ProviderStatus
    detail: str = ""
    model_count: int = 0

    @property
    def is_available(self) -> bool:
        """Whether this provider's models should be offered to callers."""
        return self.status is ProviderStatus.AVAILABLE


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn in a conversation."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """A provider-neutral completion request.

    Fields here are *intent*. Each adapter translates them into whatever the
    target model actually accepts, dropping or renaming what it must - see
    :mod:`app.services.llm.capabilities`.
    """

    model: str
    """Bare model id, without the provider prefix."""

    messages: list[ChatMessage]
    system: str | None = None
    max_output_tokens: int = 2_000
    temperature: float | None = 0.2
    effort: str | None = "medium"
    json_mode: bool = False


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """A completion, plus what had to be changed to get it."""

    text: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    param_adjustments: list[str] = field(default_factory=list)
    """Human-readable notes about parameters dropped, renamed or translated."""

    finish_reason: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Anything that can list models and answer a chat request.

    Credentials arrive per call rather than being held on the instance: they
    belong to whoever is calling, not to this service, so one provider object
    serves every caller and holds nobody's secret.
    """

    #: Stable provider key. Doubles as the namespace in model ids and as the
    #: service name this provider's credential is stored under in keyring.
    name: str

    #: Whether this provider needs a credential at all. Local runtimes do not.
    requires_credential: bool

    def is_configured(self) -> bool:
        """Whether this provider has the non-secret configuration it needs.

        Says nothing about credentials - those are resolved per caller.
        """
        ...

    async def list_models(self, auth: ResolvedAuth) -> list[ModelInfo]:
        """Return every model this provider currently offers.

        Args:
            auth: What to attach, resolved from keyring for this caller.

        Raises:
            Exception: Any failure. The registry classifies it into a
                :class:`ProviderStatus` rather than letting it escape.
        """
        ...

    async def chat(self, request: ChatRequest, auth: ResolvedAuth) -> ChatResponse:
        """Run one completion with the caller's credential."""
        ...
