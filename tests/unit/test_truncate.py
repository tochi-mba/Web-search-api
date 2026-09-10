import pytest
from hypothesis import given
from hypothesis import strategies as st

from app import constants
from app.services.text.truncate import (
    Truncation,
    char_budget_for_context,
    truncate,
)


def test_short_text_is_returned_untouched():
    result = truncate("hello world", limit=100)
    assert result == Truncation(text="hello world", truncated=False, original_chars=11)
    assert result.chars_submitted == 11


def test_long_text_is_cut_to_the_limit():
    result = truncate("x" * 500, limit=100)
    assert result.truncated is True
    assert result.original_chars == 500
    assert len(result.text) <= 100


def test_cut_prefers_a_word_boundary():
    text = "alpha beta gamma delta epsilon zeta"
    result = truncate(text, limit=20)
    assert not result.text.endswith("gam")
    assert result.text == "alpha beta gamma"


def test_cut_falls_back_to_a_hard_slice_without_usable_whitespace():
    result = truncate("x" * 50, limit=10)
    assert result.text == "x" * 10
    assert result.truncated is True


def test_boundary_search_does_not_discard_most_of_the_text():
    text = "a" * 90 + " " + "b" * 9
    result = truncate(text, limit=100)
    assert result.truncated is False


def test_exact_limit_is_not_truncation():
    result = truncate("x" * 100, limit=100)
    assert result.truncated is False
    assert result.chars_submitted == 100


def test_empty_text_is_handled():
    result = truncate("", limit=10)
    assert result.text == ""
    assert result.truncated is False


def test_zero_or_negative_limit_yields_empty_text():
    assert truncate("hello", limit=0).text == ""
    assert truncate("hello", limit=-5).text == ""
    assert truncate("hello", limit=0).truncated is True


@pytest.mark.parametrize("limit", [1, 5, 50, 500])
@given(text=st.text(min_size=0, max_size=2000))
def test_result_never_exceeds_the_limit(text, limit):
    assert len(truncate(text, limit=limit).text) <= limit


def test_char_budget_uses_the_model_context_window():
    budget = char_budget_for_context(context_window=200_000, hard_limit=1_000_000)
    usable_tokens = 200_000 - constants.RESERVED_OUTPUT_TOKENS - constants.PROMPT_OVERHEAD_TOKENS
    assert budget == usable_tokens * constants.CHARS_PER_TOKEN


def test_char_budget_never_exceeds_the_hard_limit():
    assert char_budget_for_context(context_window=1_000_000, hard_limit=40_000) == 40_000


def test_char_budget_handles_an_unknown_context_window():
    assert char_budget_for_context(context_window=None, hard_limit=40_000) == 40_000


def test_char_budget_is_never_negative():
    assert char_budget_for_context(context_window=100, hard_limit=40_000) == 0


def test_small_context_windows_truncate_harder_than_large_ones():
    small = char_budget_for_context(context_window=200_000, hard_limit=10_000_000)
    large = char_budget_for_context(context_window=1_000_000, hard_limit=10_000_000)
    assert small < large
