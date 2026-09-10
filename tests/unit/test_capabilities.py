"""The capability layer is what makes models within a provider interchangeable.

Each test below pins the exact wire body produced for a model family whose API
constraints differ from its siblings. These are the 400s this layer exists to
prevent.
"""

import pytest

from app.services.llm.capabilities import (
    MaxTokensParam,
    ModelCapabilities,
    ReasoningStyle,
    clamp_output_tokens,
    find_unsupported_parameter,
    resolve_capabilities,
    shape_openai_body,
)

MESSAGES = [{"role": "user", "content": "summarise this"}]


def shape(model, **overrides):
    caps = overrides.pop("capabilities", None) or resolve_capabilities(model)
    kwargs = {
        "model": model,
        "messages": MESSAGES,
        "system": "You are terse.",
        "max_output_tokens": 2_000,
        "temperature": 0.2,
        "effort": "medium",
        "json_mode": False,
        "capabilities": caps,
    }
    kwargs.update(overrides)
    return shape_openai_body(**kwargs)


# --- resolution ------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5", ReasoningStyle.ADAPTIVE_THINKING),
        ("claude-sonnet-5", ReasoningStyle.ADAPTIVE_THINKING),
        ("claude-fable-5-1", ReasoningStyle.ADAPTIVE_THINKING),
        ("claude-haiku-4-5", ReasoningStyle.BUDGET_TOKENS),
        ("claude-3-5-sonnet-20241022", ReasoningStyle.BUDGET_TOKENS),
        ("o3", ReasoningStyle.EFFORT),
        ("o4-mini", ReasoningStyle.EFFORT),
        ("gpt-5", ReasoningStyle.EFFORT),
        ("gpt-4o", ReasoningStyle.NONE),
        ("qwen3-32b", ReasoningStyle.ENABLE_THINKING),
        ("glm-4.6", ReasoningStyle.THINKING_TYPE),
    ],
)
def test_reasoning_style_resolution(model, expected):
    assert resolve_capabilities(model).reasoning is expected


def test_unknown_models_get_conservative_defaults():
    caps = resolve_capabilities("some-model-invented-next-year")
    assert caps.reasoning is ReasoningStyle.NONE
    assert caps.supports_json_mode is False
    assert "conservative defaults" in caps.notes[0]


def test_live_metadata_overrides_the_rule_table():
    caps = resolve_capabilities(
        "claude-opus-5", live_context_window=123_456, live_max_output_tokens=7_777
    )
    assert caps.context_window == 123_456
    assert caps.max_output_tokens == 7_777
    assert caps.reasoning is ReasoningStyle.ADAPTIVE_THINKING


def test_first_matching_rule_wins():
    # claude-opus-5 must match the Opus rule, not the generic ^claude- rule.
    assert resolve_capabilities("claude-opus-5").context_window == 1_000_000


# --- OpenAI o-series ------------------------------------------------------- #


def test_o_series_uses_max_completion_tokens_and_drops_temperature():
    result = shape("o3")
    assert "max_completion_tokens" in result.body
    assert "max_tokens" not in result.body
    assert "temperature" not in result.body
    assert result.body["reasoning_effort"] == "medium"


def test_o_series_uses_the_developer_role():
    assert shape("o3").body["messages"][0]["role"] == "developer"


def test_o_series_adjustments_are_reported():
    adjustments = " ".join(shape("o3").adjustments)
    assert "temperature dropped" in adjustments
    assert "max_completion_tokens" in adjustments


# --- OpenAI conventional --------------------------------------------------- #


def test_gpt4o_keeps_temperature_and_max_tokens():
    result = shape("gpt-4o")
    assert result.body["max_tokens"] == 2_000
    assert result.body["temperature"] == 0.2
    assert result.body["messages"][0]["role"] == "system"
    assert "reasoning_effort" not in result.body


def test_gpt4o_reports_that_effort_was_dropped():
    assert any("no reasoning controls" in a for a in shape("gpt-4o").adjustments)


def test_gpt5_drops_temperature_but_keeps_effort():
    result = shape("gpt-5")
    assert "temperature" not in result.body
    assert result.body["reasoning_effort"] == "medium"
    assert "max_completion_tokens" in result.body


# --- Anthropic ------------------------------------------------------------- #


def test_opus_5_gets_adaptive_thinking_and_never_budget_tokens():
    result = shape("claude-opus-5")
    assert result.body["thinking"] == {"type": "adaptive"}
    assert result.body["output_config"] == {"effort": "medium"}
    assert "budget_tokens" not in str(result.body)
    assert "temperature" not in result.body


def test_haiku_45_gets_budget_tokens_and_never_effort():
    result = shape("claude-haiku-4-5", max_output_tokens=8_000)
    assert result.body["thinking"]["type"] == "enabled"
    assert result.body["thinking"]["budget_tokens"] == 4_000
    assert "output_config" not in result.body
    assert result.body["temperature"] == 0.2


def test_budget_tokens_respects_the_minimum():
    result = shape("claude-haiku-4-5", max_output_tokens=4_000)
    assert result.body["thinking"]["budget_tokens"] >= 1_024


def test_thinking_is_disabled_when_the_output_budget_is_too_small():
    result = shape("claude-haiku-4-5", max_output_tokens=1_000)
    assert "thinking" not in result.body
    assert any("too small" in a for a in result.adjustments)


# --- other families -------------------------------------------------------- #


def test_deepseek_reasoner_drops_sampling_parameters():
    result = shape("deepseek-reasoner")
    assert "temperature" not in result.body
    assert any("temperature dropped" in a for a in result.adjustments)


def test_deepseek_chat_keeps_temperature():
    assert shape("deepseek-chat").body["temperature"] == 0.2


def test_qwen_uses_enable_thinking():
    result = shape("qwen3-32b")
    assert result.body["enable_thinking"] is True
    assert any("enable_thinking" in a for a in result.adjustments)


def test_glm_uses_a_thinking_type_object():
    result = shape("glm-4.6")
    assert result.body["thinking"] == {"type": "enabled"}


def test_unrecognised_effort_maps_to_medium_for_openai_style():
    result = shape("o3", effort="xhigh")
    assert result.body["reasoning_effort"] == "medium"
    assert any("mapped to 'medium'" in a for a in result.adjustments)


# --- json mode, system prompts and clamping -------------------------------- #


def test_json_mode_is_requested_when_supported():
    assert shape("gpt-4o", json_mode=True).body["response_format"] == {"type": "json_object"}


def test_json_mode_is_dropped_when_unsupported():
    result = shape("sonar", json_mode=True)
    assert "response_format" not in result.body
    assert any("JSON mode dropped" in a for a in result.adjustments)


def test_system_prompt_is_folded_in_when_the_model_has_no_system_role():
    caps = ModelCapabilities(supports_system_prompt=False)
    result = shape("whatever", capabilities=caps)
    assert result.body["messages"][0]["role"] == "user"
    assert any("folded into" in a for a in result.adjustments)


def test_absent_system_prompt_produces_only_the_user_turn():
    assert len(shape("gpt-4o", system=None).body["messages"]) == 1


def test_output_tokens_are_clamped_to_the_model_cap():
    result = shape("gpt-4", max_output_tokens=999_999)
    assert result.body["max_tokens"] == 4_096
    assert any("reduced from" in a for a in result.adjustments)


def test_clamp_is_a_no_op_below_the_cap():
    caps = ModelCapabilities(max_output_tokens=1000)
    assert clamp_output_tokens(500, caps) == (500, None)


def test_clamp_without_a_known_cap_is_a_no_op():
    assert clamp_output_tokens(500, ModelCapabilities()) == (500, None)


def test_no_effort_requested_adds_no_note():
    assert shape("gpt-4o", effort=None).adjustments == []


def test_temperature_none_is_simply_omitted():
    assert "temperature" not in shape("gpt-4o", temperature=None).body


# --- self-healing 400 recovery --------------------------------------------- #


@pytest.mark.parametrize(
    "message",
    [
        "Unsupported parameter: 'temperature' is not supported with this model.",
        "Unrecognized request argument: temperature",
        "'temperature' is not supported",
        "Invalid parameter: temperature",
        "Parameter 'temperature' is not supported",
    ],
)
def test_offending_parameter_is_recovered_from_the_error_text(message):
    body = {"model": "x", "temperature": 0.2}
    assert find_unsupported_parameter(message, body) == "temperature"


def test_dotted_parameter_paths_resolve_to_the_top_level_key():
    body = {"thinking": {"type": "adaptive"}}
    assert find_unsupported_parameter("Unsupported parameter: 'thinking.type'", body) == "thinking"


def test_parameters_not_present_in_the_body_are_ignored():
    assert find_unsupported_parameter("Unsupported parameter: 'nonsense'", {"model": "x"}) is None


def test_unparseable_errors_yield_nothing():
    assert find_unsupported_parameter("Internal server error", {"model": "x"}) is None


def test_max_tokens_param_enum_values():
    assert MaxTokensParam.MAX_TOKENS.value == "max_tokens"
    assert MaxTokensParam.MAX_COMPLETION_TOKENS.value == "max_completion_tokens"
