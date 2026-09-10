"""The model-id pattern table driving capability resolution.

Ordered most-specific first; the first match wins. Every entry encodes a real
API constraint that would otherwise produce a 400, and each is covered by a test
asserting the exact wire body produced for that family.
"""

from __future__ import annotations

from app.services.llm.capabilities import (
    CapabilityRule,
    MaxTokensParam,
    ModelCapabilities,
    ReasoningStyle,
    _rule,
)

#: Anthropic's 1M-context generation.
_ANTHROPIC_1M = 1_000_000

CAPABILITY_RULES: tuple[CapabilityRule, ...] = (
    # ---------------------------------------------------------------- Anthropic
    _rule(
        r"^claude-(fable|mythos)-5",
        "Claude Fable/Mythos 5.x: thinking always on, no temperature, no budget_tokens",
        supports_temperature=False,
        supports_top_p=False,
        reasoning=ReasoningStyle.ADAPTIVE_THINKING,
        context_window=_ANTHROPIC_1M,
        max_output_tokens=128_000,
        notes=("thinking is always on", "temperature and budget_tokens are rejected"),
    ),
    _rule(
        r"^claude-opus-(5|4-8|4-7|4-6)",
        "Claude Opus 4.6+: adaptive thinking and effort, no temperature",
        supports_temperature=False,
        supports_top_p=False,
        reasoning=ReasoningStyle.ADAPTIVE_THINKING,
        context_window=_ANTHROPIC_1M,
        max_output_tokens=128_000,
        notes=("budget_tokens was removed on this model",),
    ),
    _rule(
        r"^claude-sonnet-(5|4-6)",
        "Claude Sonnet 4.6+: adaptive thinking and effort, no temperature",
        supports_temperature=False,
        supports_top_p=False,
        reasoning=ReasoningStyle.ADAPTIVE_THINKING,
        context_window=_ANTHROPIC_1M,
        max_output_tokens=128_000,
    ),
    _rule(
        r"^claude-haiku-4-5",
        "Claude Haiku 4.5: budget_tokens style thinking, temperature allowed",
        supports_temperature=True,
        reasoning=ReasoningStyle.BUDGET_TOKENS,
        context_window=200_000,
        max_output_tokens=64_000,
        notes=("effort is rejected on this model; thinking uses budget_tokens",),
    ),
    _rule(
        r"^claude-",
        "Older Claude models: budget_tokens style thinking",
        reasoning=ReasoningStyle.BUDGET_TOKENS,
        context_window=200_000,
        max_output_tokens=8_192,
    ),
    # ------------------------------------------------------------------- OpenAI
    _rule(
        r"^(o1|o3|o4)(-|$)",
        "OpenAI o-series reasoning models: no temperature, max_completion_tokens, developer role",
        supports_temperature=False,
        supports_top_p=False,
        supports_system_prompt=True,
        system_role="developer",
        max_tokens_param=MaxTokensParam.MAX_COMPLETION_TOKENS,
        reasoning=ReasoningStyle.EFFORT,
        context_window=200_000,
        max_output_tokens=100_000,
        notes=("sampling parameters are rejected", "system role is 'developer'"),
    ),
    _rule(
        r"^gpt-5",
        "GPT-5 family: reasoning_effort, max_completion_tokens",
        supports_temperature=False,
        supports_top_p=False,
        max_tokens_param=MaxTokensParam.MAX_COMPLETION_TOKENS,
        reasoning=ReasoningStyle.EFFORT,
        context_window=400_000,
        max_output_tokens=128_000,
    ),
    _rule(
        r"^gpt-4o",
        "GPT-4o: conventional chat parameters",
        context_window=128_000,
        max_output_tokens=16_384,
    ),
    _rule(
        r"^gpt-4\.1",
        "GPT-4.1: conventional chat parameters, long context",
        context_window=1_047_576,
        max_output_tokens=32_768,
    ),
    _rule(
        r"^gpt-4",
        "GPT-4: conventional chat parameters",
        context_window=128_000,
        max_output_tokens=4_096,
    ),
    _rule(
        r"^gpt-3\.5",
        "GPT-3.5: conventional chat parameters, small context",
        context_window=16_385,
        max_output_tokens=4_096,
    ),
    # ----------------------------------------------------------------- DeepSeek
    _rule(
        r"deepseek-(reasoner|r1)",
        "DeepSeek reasoner: rejects sampling parameters",
        supports_temperature=False,
        supports_top_p=False,
        supports_json_mode=False,
        context_window=128_000,
        max_output_tokens=8_192,
        notes=("temperature, top_p and penalties are ignored or rejected",),
    ),
    _rule(
        r"^deepseek",
        "DeepSeek chat models",
        context_window=128_000,
        max_output_tokens=8_192,
    ),
    # -------------------------------------------------------------------- Qwen
    _rule(
        r"^(qwen|qwq)",
        "Qwen: thinking is toggled with enable_thinking",
        reasoning=ReasoningStyle.ENABLE_THINKING,
        context_window=131_072,
        max_output_tokens=8_192,
    ),
    # --------------------------------------------------------------- Z.AI / GLM
    _rule(
        r"^glm",
        "GLM: thinking is toggled with a thinking.type object",
        reasoning=ReasoningStyle.THINKING_TYPE,
        context_window=128_000,
        max_output_tokens=8_192,
    ),
    # ------------------------------------------------------------------ Mistral
    _rule(
        r"^magistral",
        "Magistral reasoning models: reduced parameter surface",
        supports_top_p=False,
        supports_json_mode=False,
        context_window=128_000,
        max_output_tokens=8_192,
    ),
    # --------------------------------------------------------------- Perplexity
    _rule(
        r"^sonar",
        "Perplexity Sonar: search-backed, no JSON mode",
        supports_json_mode=False,
        context_window=128_000,
        max_output_tokens=4_096,
    ),
    # -------------------------------------------------------------------- Gemini
    _rule(
        r"^gemini-",
        "Gemini via the OpenAI compatibility shim",
        context_window=1_000_000,
        max_output_tokens=8_192,
    ),
    # ------------------------------------------------------------ Llama / local
    _rule(
        r"^(llama|mixtral|mistral|gemma|phi|codellama)",
        "Common open-weight families served locally or by inference vendors",
        context_window=128_000,
        max_output_tokens=4_096,
    ),
)

#: Exposed for tests and documentation.
DEFAULT_CAPABILITIES = ModelCapabilities()
