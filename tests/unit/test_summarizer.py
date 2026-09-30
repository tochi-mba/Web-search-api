import pytest

from app.services.keyring.client import NO_AUTH
from app.services.llm.base import ChatResponse, ModelInfo
from app.services.llm.registry import ModelRegistry
from app.services.llm.summarizer import Summarizer

SUMMARY_JSON = '{"executive_summary": "Latency rose 40%.", "key_points": ["queues", "sharding"]}'


class RecordingProvider:
    """A provider that records requests and returns a canned completion."""

    def __init__(self, name="fake", models=None, response_text=SUMMARY_JSON):
        self.name = name
        self._models = models or [("test-model", 1_000_000)]
        self.response_text = response_text
        self.requests = []

    #: Stubs stand in for keyless providers, so no keyring is involved.
    requires_credential = False

    def is_configured(self):
        return True

    async def list_models(self, auth=NO_AUTH):
        return [
            ModelInfo.build(self.name, model, context_window=window)
            for model, window in self._models
        ]

    async def chat(self, request, auth=NO_AUTH):
        self.requests.append(request)
        return ChatResponse(
            text=self.response_text,
            model=request.model,
            provider=self.name,
            input_tokens=500,
            output_tokens=60,
            param_adjustments=["temperature dropped: rejected by this model"],
        )


def build(provider, **kwargs):
    registry = ModelRegistry(
        [provider],
        default_model=f"{provider.name}:{provider._models[0][0]}",
        cache_ttl_seconds=300.0,
    )
    return Summarizer(registry, **kwargs)


@pytest.fixture
def provider():
    return RecordingProvider()


# --- happy path ------------------------------------------------------------ #


async def test_summary_is_returned(provider):
    summary = await build(provider).summarize("Widget latency rose across three releases.")
    assert summary.executive_summary == "Latency rose 40%."
    assert summary.key_points == ["queues", "sharding"]
    assert summary.model == "fake:test-model"
    assert summary.provider == "fake"


async def test_usage_is_reported(provider):
    summary = await build(provider).summarize("Some content.")
    assert summary.input_tokens == 500
    assert summary.output_tokens == 60


async def test_param_adjustments_are_surfaced(provider):
    summary = await build(provider).summarize("Some content.")
    assert summary.param_adjustments == ["temperature dropped: rejected by this model"]


async def test_json_mode_is_requested(provider):
    await build(provider).summarize("Some content.")
    assert provider.requests[0].json_mode is True


async def test_content_reaches_the_model(provider):
    await build(provider).summarize("A very specific sentence about widgets.")
    assert "A very specific sentence about widgets." in provider.requests[0].messages[0].content


# --- notes and topic ------------------------------------------------------- #


async def test_additional_notes_reach_the_model_and_are_flagged(provider):
    summary = await build(provider).summarize(
        "Content.", additional_notes="Focus on cost, not latency."
    )
    assert "Focus on cost, not latency." in provider.requests[0].messages[0].content
    assert summary.notes_applied is True


async def test_absent_notes_are_flagged_as_not_applied(provider):
    assert (await build(provider).summarize("Content.")).notes_applied is False


async def test_topic_reaches_the_model(provider):
    await build(provider).summarize("Content.", topic="widget latency")
    assert "widget latency" in provider.requests[0].messages[0].content


async def test_sources_reach_the_model(provider):
    await build(provider).summarize("Content.", sources=["https://a.com/x"])
    assert "https://a.com/x" in provider.requests[0].messages[0].content


# --- truncation ------------------------------------------------------------ #


async def test_short_content_is_not_truncated(provider):
    summary = await build(provider).summarize("Short.")
    assert summary.truncated is False
    assert summary.chars_submitted == len("Short.")
    assert summary.original_chars == len("Short.")


async def test_long_content_is_truncated_and_reported(provider):
    content = "word " * 50_000
    summary = await build(provider, max_content_chars=1_000).summarize(content)
    assert summary.truncated is True
    assert summary.chars_submitted <= 1_000
    assert summary.original_chars == len(content)


async def test_a_small_context_model_truncates_harder_than_a_large_one():
    small = RecordingProvider("small", models=[("tiny-ctx", 200_000)])
    large = RecordingProvider("large", models=[("big-ctx", 1_000_000)])
    content = "word " * 500_000

    small_summary = await build(small, max_content_chars=10_000_000).summarize(content)
    large_summary = await build(large, max_content_chars=10_000_000).summarize(content)

    assert small_summary.chars_submitted < large_summary.chars_submitted


async def test_the_hard_limit_still_applies_to_a_huge_context_model():
    provider = RecordingProvider("big", models=[("huge-ctx", 10_000_000)])
    content = "word " * 200_000
    summary = await build(provider, max_content_chars=5_000).summarize(content)
    assert summary.chars_submitted <= 5_000


# --- degenerate input ------------------------------------------------------ #


async def test_empty_content_skips_the_model_call(provider):
    """Spending a model call to be told there is nothing to summarise is waste."""
    summary = await build(provider).summarize("")
    assert summary.executive_summary == ""
    assert summary.chars_submitted == 0
    assert provider.requests == []


async def test_whitespace_only_content_skips_the_model_call(provider):
    summary = await build(provider).summarize("   \n\n   ")
    assert provider.requests == []
    assert summary.executive_summary == ""


async def test_notes_are_still_reported_for_empty_content(provider):
    summary = await build(provider).summarize("", additional_notes="Focus on cost.")
    assert summary.notes_applied is True


# --- model selection ------------------------------------------------------- #


async def test_explicit_model_is_used():
    provider = RecordingProvider("multi", models=[("model-a", 200_000), ("model-b", 200_000)])
    summary = await build(provider).summarize("Content.", model_id="multi:model-b")
    assert summary.model == "multi:model-b"
    assert provider.requests[0].model == "model-b"


async def test_unknown_model_raises(provider):
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await build(provider).summarize("Content.", model_id="fake:nope")


# --- sloppy model output --------------------------------------------------- #


async def test_prose_response_still_produces_a_summary():
    provider = RecordingProvider(response_text="The latency rose sharply this quarter.")
    summary = await build(provider).summarize("Content.")
    assert summary.executive_summary == "The latency rose sharply this quarter."
    assert summary.key_points == []


async def test_fenced_response_is_parsed():
    provider = RecordingProvider(response_text=f"```json\n{SUMMARY_JSON}\n```")
    summary = await build(provider).summarize("Content.")
    assert summary.executive_summary == "Latency rose 40%."


async def test_empty_model_response_yields_an_empty_summary():
    provider = RecordingProvider(response_text="")
    summary = await build(provider).summarize("Content.")
    assert summary.executive_summary == ""
