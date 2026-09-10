"""Per-model capability resolution and request shaping.

Models within a single provider are **not** interchangeable. OpenAI's reasoning
models reject ``temperature`` and rename ``max_tokens`` to
``max_completion_tokens``; Anthropic's Opus 5 rejects ``budget_tokens`` while
Haiku 4.5 requires it; DeepSeek's reasoner rejects ``temperature`` outright.
Sending one request shape per provider therefore 400s on a large slice of the
catalogue.

This module resolves what a specific model accepts and shapes the outgoing
request accordingly, recording every adjustment so the behaviour is observable
rather than mysterious.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum


class MaxTokensParam(StrEnum):
    """Which parameter name carries the output-token cap."""

    MAX_TOKENS = "max_tokens"
    MAX_COMPLETION_TOKENS = "max_completion_tokens"


class ReasoningStyle(StrEnum):
    """How a model expects reasoning depth to be expressed, if at all."""

    NONE = "none"
    """The model has no reasoning controls."""

    EFFORT = "effort"
    """OpenAI-style ``reasoning_effort``."""

    ADAPTIVE_THINKING = "adaptive_thinking"
    """Anthropic 4.6+ style ``thinking={"type": "adaptive"}`` plus ``effort``."""

    BUDGET_TOKENS = "budget_tokens"
    """Anthropic pre-4.6 style ``thinking={"type": "enabled", "budget_tokens": N}``."""

    ENABLE_THINKING = "enable_thinking"
    """Qwen/DashScope style boolean toggle."""

    THINKING_TYPE = "thinking_type"
    """GLM/Z.AI style ``thinking={"type": "enabled"}``."""


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """What one model will and will not accept."""

    supports_temperature: bool = True
    supports_top_p: bool = True
    supports_streaming: bool = True
    supports_json_mode: bool = True
    supports_system_prompt: bool = True
    system_role: str = "system"
    max_tokens_param: MaxTokensParam = MaxTokensParam.MAX_TOKENS
    reasoning: ReasoningStyle = ReasoningStyle.NONE
    context_window: int | None = None
    max_output_tokens: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CapabilityRule:
    """A model-id pattern and the capabilities it implies."""

    pattern: re.Pattern[str]
    capabilities: ModelCapabilities
    description: str


#: Conservative fallback for models we have never heard of: send the smallest
#: universally-accepted request rather than guessing at exotic parameters.
UNKNOWN_MODEL_CAPABILITIES = ModelCapabilities(
    supports_temperature=True,
    supports_top_p=True,
    supports_json_mode=False,
    reasoning=ReasoningStyle.NONE,
    notes=("unknown model: using conservative defaults",),
)


def _rule(pattern: str, description: str, **kwargs: object) -> CapabilityRule:
    """Build a capability rule from a regex and capability overrides."""
    return CapabilityRule(
        pattern=re.compile(pattern, re.IGNORECASE),
        capabilities=ModelCapabilities(**kwargs),  # type: ignore[arg-type]
        description=description,
    )


def resolve_capabilities(
    model: str,
    *,
    live_context_window: int | None = None,
    live_max_output_tokens: int | None = None,
) -> ModelCapabilities:
    """Resolve what ``model`` accepts.

    Three layers, most specific first: live metadata reported by the provider,
    then the pattern rules, then conservative defaults for models nobody has
    catalogued yet.

    Args:
        model: The bare model id, without a provider prefix.
        live_context_window: Context window reported by the provider, if any.
        live_max_output_tokens: Output cap reported by the provider, if any.

    Returns:
        The resolved capabilities.
    """
    from app.services.llm.capability_rules import CAPABILITY_RULES

    resolved = UNKNOWN_MODEL_CAPABILITIES
    for rule in CAPABILITY_RULES:
        if rule.pattern.search(model):
            resolved = rule.capabilities
            break

    # Live metadata always beats a hardcoded guess.
    if live_context_window is not None:
        resolved = replace(resolved, context_window=live_context_window)
    if live_max_output_tokens is not None:
        resolved = replace(resolved, max_output_tokens=live_max_output_tokens)

    return resolved


@dataclass(frozen=True, slots=True)
class ShapedRequest:
    """An outgoing request body plus the adjustments made to produce it."""

    body: dict[str, object]
    adjustments: list[str]


def clamp_output_tokens(requested: int, capabilities: ModelCapabilities) -> tuple[int, str | None]:
    """Clamp an output-token request to what the model actually allows."""
    cap = capabilities.max_output_tokens
    if cap is not None and requested > cap:
        return cap, f"max output tokens reduced from {requested} to the model's cap of {cap}"
    return requested, None


#: Effort levels understood by OpenAI-style ``reasoning_effort``.
_OPENAI_EFFORTS = frozenset({"minimal", "low", "medium", "high"})

#: Fraction of the output budget given to thinking on budget_tokens models.
_BUDGET_TOKENS_FRACTION = 0.5

#: Anthropic requires a thinking budget of at least this many tokens.
_MIN_BUDGET_TOKENS = 1_024


def apply_reasoning(
    body: dict[str, object],
    *,
    effort: str | None,
    capabilities: ModelCapabilities,
    max_output_tokens: int,
    adjustments: list[str],
) -> None:
    """Express the requested reasoning depth in whatever dialect the model speaks.

    Mutates ``body`` in place and appends a note whenever the request had to be
    translated or dropped.
    """
    style = capabilities.reasoning

    if effort is None or style is ReasoningStyle.NONE:
        if effort is not None:
            adjustments.append("effort dropped: this model has no reasoning controls")
        return

    if style is ReasoningStyle.EFFORT:
        level = effort if effort in _OPENAI_EFFORTS else "medium"
        if level != effort:
            adjustments.append(f"effort '{effort}' mapped to '{level}' for this model")
        body["reasoning_effort"] = level
        return

    if style is ReasoningStyle.ADAPTIVE_THINKING:
        body["thinking"] = {"type": "adaptive"}
        body["output_config"] = {"effort": effort}
        return

    if style is ReasoningStyle.BUDGET_TOKENS:
        budget = max(int(max_output_tokens * _BUDGET_TOKENS_FRACTION), _MIN_BUDGET_TOKENS)
        if budget >= max_output_tokens:
            # The budget must stay strictly below the output cap, and a cap this
            # small leaves no room to think usefully.
            adjustments.append("thinking disabled: output budget too small for a thinking budget")
            return
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        adjustments.append(f"effort '{effort}' translated to budget_tokens={budget}")
        return

    if style is ReasoningStyle.ENABLE_THINKING:
        body["enable_thinking"] = True
        adjustments.append(f"effort '{effort}' translated to enable_thinking=true")
        return

    body["thinking"] = {"type": "enabled"}
    adjustments.append(f"effort '{effort}' translated to thinking.type=enabled")


def shape_openai_body(
    *,
    model: str,
    messages: list[dict[str, str]],
    system: str | None,
    max_output_tokens: int,
    temperature: float | None,
    effort: str | None,
    json_mode: bool,
    capabilities: ModelCapabilities,
) -> ShapedRequest:
    """Build an OpenAI-style request body that the target model will accept.

    Unsupported parameters are dropped, renamed or translated rather than sent
    and rejected. Every change is recorded in :attr:`ShapedRequest.adjustments`.
    """
    adjustments: list[str] = []

    payload_messages: list[dict[str, str]] = []
    if system:
        if capabilities.supports_system_prompt:
            payload_messages.append({"role": capabilities.system_role, "content": system})
            if capabilities.system_role != "system":
                adjustments.append(f"system prompt sent with role '{capabilities.system_role}'")
        else:
            # Fold the system prompt into the first user turn rather than lose it.
            payload_messages.append({"role": "user", "content": system})
            adjustments.append("system prompt folded into the first user message")
    payload_messages.extend(messages)

    tokens, note = clamp_output_tokens(max_output_tokens, capabilities)
    if note:
        adjustments.append(note)

    body: dict[str, object] = {
        "model": model,
        "messages": payload_messages,
        capabilities.max_tokens_param.value: tokens,
    }
    if capabilities.max_tokens_param is not MaxTokensParam.MAX_TOKENS:
        adjustments.append(
            f"max_tokens renamed to {capabilities.max_tokens_param.value} for this model"
        )

    if temperature is not None:
        if capabilities.supports_temperature:
            body["temperature"] = temperature
        else:
            adjustments.append("temperature dropped: rejected by this model")

    if json_mode:
        if capabilities.supports_json_mode:
            body["response_format"] = {"type": "json_object"}
        else:
            adjustments.append("JSON mode dropped: unsupported by this model")

    apply_reasoning(
        body,
        effort=effort,
        capabilities=capabilities,
        max_output_tokens=tokens,
        adjustments=adjustments,
    )

    return ShapedRequest(body=body, adjustments=adjustments)


#: Parameter names recovered from a provider's 400 message, so an unknown model
#: released after this code was written degrades to a working request rather
#: than failing outright.
_UNSUPPORTED_PARAM_PATTERNS = (
    re.compile(r"[Uu]nsupported (?:parameter|value)[: ]+'?([a-z_.]+)'?"),
    re.compile(r"[Uu]nrecognized (?:request )?argument[: ]+'?([a-z_.]+)'?"),
    re.compile(r"'?([a-z_.]+)'? is not supported"),
    re.compile(r"[Ee]xtra inputs are not permitted.*'([a-z_.]+)'"),
    re.compile(r"[Ii]nvalid parameter[: ]+'?([a-z_.]+)'?"),
    re.compile(r"[Pp]arameter '([a-z_.]+)' is not supported"),
)


def find_unsupported_parameter(message: str, body: Mapping[str, object]) -> str | None:
    """Extract the offending parameter name from a provider's 400 message.

    Args:
        message: The error text the provider returned.
        body: The request body that was rejected. Read-only.

    Returns:
        A top-level key of ``body`` that the provider named, or ``None``.
    """
    for pattern in _UNSUPPORTED_PARAM_PATTERNS:
        match = pattern.search(message)
        if match:
            # Providers sometimes report a dotted path such as "thinking.type".
            candidate = match.group(1).split(".")[0]
            if candidate in body:
                return candidate
    return None
