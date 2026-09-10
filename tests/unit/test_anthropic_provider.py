"""Anthropic adapter tests, run against a real local HTTP server.

The SDK is built on httpx2, which respx cannot patch, so these tests point the
client at a recording server and assert the bytes that actually go on the wire.
That is stronger than stubbing SDK methods: the whole reason this adapter exists
is that Opus 5 and Haiku 4.5 require different request bodies.
"""

import pytest

from app.core.errors import (
    ProviderUnavailableError,
    RateLimitedError,
    UpstreamError,
)
from app.services.llm.base import ChatMessage, ChatRequest
from app.services.llm.providers.anthropic import AnthropicProvider
from tests.mock_api_server import MockAPIServer


@pytest.fixture
def server():
    api = MockAPIServer()
    try:
        yield api
    finally:
        api.close()


@pytest.fixture
async def provider(server):
    api = AnthropicProvider(
        api_key="test-key",
        base_url=server.base_url,
        timeout_seconds=5.0,
        max_retries=0,
    )
    try:
        yield api
    finally:
        await api.aclose()


def request_for(model="claude-opus-5", **kwargs):
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role="user", content="summarise this")],
        system="Be terse.",
        **kwargs,
    )


def message_response(text="An executive summary.", stop_reason="end_turn"):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }


def models_response(*entries):
    return {"data": list(entries), "has_more": False, "first_id": None, "last_id": None}


# --- configuration --------------------------------------------------------- #


def test_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert AnthropicProvider().is_configured() is True


def test_missing_key_means_not_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert AnthropicProvider().is_configured() is False


def test_client_is_built_once(provider):
    assert provider.client is provider.client


def test_base_url_override_is_applied(server):
    provider = AnthropicProvider(api_key="k", base_url=server.base_url)
    assert server.base_url in str(provider.client.base_url)


def test_default_base_url_is_the_real_api():
    assert "api.anthropic.com" in str(AnthropicProvider(api_key="k").client.base_url)


# --- listing models -------------------------------------------------------- #


async def test_models_are_listed_and_namespaced(provider, server):
    server.queue(
        models_response(
            {
                "type": "model",
                "id": "claude-opus-5",
                "display_name": "Claude Opus 5",
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
    )
    models = await provider.list_models()
    assert models[0].id == "anthropic:claude-opus-5"
    assert models[0].display_name == "Claude Opus 5"
    # Falls back to the capability table when the API omits the window.
    assert models[0].context_window == 1_000_000


async def test_live_metadata_beats_the_capability_table(provider, server):
    server.queue(
        models_response(
            {
                "type": "model",
                "id": "claude-opus-5",
                "display_name": "Claude Opus 5",
                "created_at": "2026-01-01T00:00:00Z",
                "max_input_tokens": 555_000,
                "max_tokens": 9_999,
            }
        )
    )
    model = (await provider.list_models())[0]
    assert model.context_window == 555_000
    assert model.max_output_tokens == 9_999


async def test_the_api_key_is_sent(provider, server):
    server.queue(models_response())
    await provider.list_models()
    assert server.last_request.headers["x-api-key"] == "test-key"


async def test_bad_key_maps_to_provider_unavailable(provider, server):
    server.queue(
        {"type": "error", "error": {"type": "authentication_error", "message": "invalid key"}},
        status=401,
    )
    with pytest.raises(ProviderUnavailableError, match="rejected the credential"):
        await provider.list_models()


async def test_rate_limit_maps_to_rate_limited(provider, server):
    server.queue({"type": "error", "error": {"type": "rate_limit_error"}}, status=429)
    with pytest.raises(RateLimitedError):
        await provider.list_models()


async def test_server_error_maps_to_provider_unavailable(provider, server):
    server.queue({"type": "error", "error": {"type": "api_error"}}, status=500)
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await provider.list_models()


# --- chat body shaping ----------------------------------------------------- #


async def test_opus_5_sends_adaptive_thinking_and_no_temperature(provider, server):
    server.queue(message_response())
    await provider.chat(request_for("claude-opus-5"))

    body = server.last_request.body
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "medium"}
    assert "temperature" not in body
    assert "budget_tokens" not in str(body)
    assert body["system"] == "Be terse."


async def test_haiku_45_sends_budget_tokens_and_keeps_temperature(provider, server):
    server.queue(message_response())
    await provider.chat(request_for("claude-haiku-4-5", max_output_tokens=8_000))

    body = server.last_request.body
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4_000}
    assert "output_config" not in body
    assert body["temperature"] == 0.2


async def test_thinking_is_omitted_when_the_budget_is_too_small(provider, server):
    server.queue(message_response())
    response = await provider.chat(request_for("claude-haiku-4-5", max_output_tokens=1_000))

    assert "thinking" not in server.last_request.body
    assert any("too small" in a for a in response.param_adjustments)


async def test_no_effort_requested_sends_no_thinking(provider, server):
    server.queue(message_response())
    await provider.chat(request_for("claude-opus-5", effort=None))
    assert "thinking" not in server.last_request.body


async def test_no_temperature_requested_is_omitted(provider, server):
    server.queue(message_response())
    await provider.chat(request_for("claude-haiku-4-5", temperature=None))
    assert "temperature" not in server.last_request.body


async def test_effort_is_dropped_for_a_model_without_reasoning_controls(server):
    """A model resolving to no reasoning style must not receive thinking config."""
    from unittest.mock import patch

    from app.services.llm.capabilities import ModelCapabilities

    provider = AnthropicProvider(
        api_key="k", base_url=server.base_url, timeout_seconds=5.0, max_retries=0
    )
    server.queue(message_response())
    with patch(
        "app.services.llm.providers.anthropic.resolve_capabilities",
        return_value=ModelCapabilities(),
    ):
        response = await provider.chat(request_for("claude-plain"))

    assert "thinking" not in server.last_request.body
    assert any("no reasoning controls" in a for a in response.param_adjustments)


async def test_output_tokens_are_clamped_to_the_model_cap(provider, server):
    server.queue(message_response())
    response = await provider.chat(request_for("claude-haiku-4-5", max_output_tokens=999_999))
    assert server.last_request.body["max_tokens"] == 64_000
    assert any("reduced from" in a for a in response.param_adjustments)


async def test_system_prompt_is_omitted_when_absent(provider, server):
    server.queue(message_response())
    await provider.chat(
        ChatRequest(model="claude-opus-5", messages=[ChatMessage("user", "hi")], system=None)
    )
    assert "system" not in server.last_request.body


# --- responses ------------------------------------------------------------- #


async def test_text_and_usage_are_returned(provider, server):
    server.queue(message_response())
    response = await provider.chat(request_for())
    assert response.text == "An executive summary."
    assert response.input_tokens == 120
    assert response.output_tokens == 30
    assert response.provider == "anthropic"
    assert response.finish_reason == "end_turn"


async def test_multiple_text_blocks_are_concatenated(provider, server):
    payload = message_response()
    payload["content"] = [
        {"type": "text", "text": "First part. "},
        {"type": "text", "text": "Second part."},
    ]
    server.queue(payload)
    assert (await provider.chat(request_for())).text == "First part. Second part."


async def test_thinking_blocks_are_excluded_from_the_text(provider, server):
    payload = message_response()
    payload["content"] = [
        {"type": "thinking", "thinking": "internal reasoning", "signature": "sig"},
        {"type": "text", "text": "The answer."},
    ]
    server.queue(payload)
    assert (await provider.chat(request_for())).text == "The answer."


async def test_a_refusal_is_surfaced_rather_than_returned_as_empty(provider, server):
    """A refusal arrives as a 200 with no usable content and must not look like success."""
    payload = message_response(text="", stop_reason="refusal")
    payload["content"] = []
    server.queue(payload)
    with pytest.raises(UpstreamError, match="declined the request"):
        await provider.chat(request_for())


async def test_chat_auth_failure_maps_to_provider_unavailable(provider, server):
    server.queue({"type": "error", "error": {"type": "authentication_error"}}, status=401)
    with pytest.raises(ProviderUnavailableError):
        await provider.chat(request_for())


async def test_chat_rate_limit_maps_to_rate_limited(provider, server):
    server.queue({"type": "error", "error": {"type": "rate_limit_error"}}, status=429)
    with pytest.raises(RateLimitedError):
        await provider.chat(request_for())


async def test_chat_server_error_maps_to_upstream(provider, server):
    server.queue({"type": "error", "error": {"type": "api_error"}}, status=500)
    with pytest.raises(UpstreamError, match="request failed"):
        await provider.chat(request_for())


async def test_aclose_is_safe_before_the_client_is_built(server):
    provider = AnthropicProvider(api_key="k", base_url=server.base_url)
    await provider.aclose()
    await provider.aclose()


async def test_list_models_timeout_maps_to_timeout_problem(server):
    """A slow provider must surface as a timeout, not a generic failure."""
    from app.core.errors import TimeoutProblem

    provider = AnthropicProvider(
        api_key="k", base_url=server.base_url, timeout_seconds=0.05, max_retries=0
    )
    server.queue(models_response(), delay_seconds=0.4)
    try:
        with pytest.raises(TimeoutProblem, match="timed out"):
            await provider.list_models()
    finally:
        await provider.aclose()


async def test_chat_timeout_maps_to_timeout_problem(server):
    from app.core.errors import TimeoutProblem

    provider = AnthropicProvider(
        api_key="k", base_url=server.base_url, timeout_seconds=0.05, max_retries=0
    )
    server.queue(message_response(), delay_seconds=0.4)
    try:
        with pytest.raises(TimeoutProblem, match="timed out"):
            await provider.chat(request_for())
    finally:
        await provider.aclose()
