"""The truncation thresholds are part of the public contract, so pin them."""

from app import constants


def test_content_threshold_is_sane():
    assert constants.MAX_CONTENT_CHARS == 40_000
    assert constants.MAX_CONTENT_CHARS > constants.MAX_NOTES_CHARS


def test_notes_threshold_is_sane():
    assert constants.MAX_NOTES_CHARS == 4_000


def test_default_model_is_namespaced():
    provider, _, model = constants.DEFAULT_MODEL.partition(":")
    assert provider == "anthropic"
    assert model == "claude-opus-5"


def test_reserved_output_and_overhead_leave_room():
    assert constants.RESERVED_OUTPUT_TOKENS > 0
    assert constants.PROMPT_OVERHEAD_TOKENS > 0


def test_chars_per_token_is_a_positive_estimate():
    assert constants.CHARS_PER_TOKEN >= 1
