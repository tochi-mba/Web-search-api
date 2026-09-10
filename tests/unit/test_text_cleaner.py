from hypothesis import given
from hypothesis import strategies as st

from app.services.text.cleaner import clean_text, collapse_whitespace

ZWSP = "\u200b"
NBSP = "\u00a0"


def test_collapses_runs_of_spaces_and_tabs():
    assert collapse_whitespace("a  \t  b") == "a b"


def test_preserves_paragraph_breaks_but_collapses_bigger_gaps():
    assert collapse_whitespace("a\n\n\n\n\nb") == "a\n\nb"


def test_strips_leading_and_trailing_whitespace():
    assert collapse_whitespace("   hi   ") == "hi"


def test_removes_zero_width_characters():
    assert clean_text(f"caf{ZWSP}e test") == "cafe test"


def test_treats_non_breaking_space_as_a_space():
    assert clean_text(f"a{NBSP}b") == "a b"


def test_normalises_smart_quotes_and_dashes():
    assert clean_text("\u201chello\u201d \u2014 world\u2019s") == '"hello" - world\'s'


def test_drops_boilerplate_lines():
    raw = "\n".join(
        [
            "Real content here that matters.",
            "Accept all cookies",
            "More real content follows.",
            "Skip to main content",
            "Subscribe to our newsletter",
        ]
    )
    cleaned = clean_text(raw)
    assert "Real content here" in cleaned
    assert "More real content" in cleaned
    assert "cookies" not in cleaned.lower()
    assert "Skip to main content" not in cleaned


def test_drops_very_short_noise_lines_between_content():
    raw = "A genuinely long sentence of content.\n|\n>\nAnother genuine sentence here."
    cleaned = clean_text(raw)
    assert cleaned == "A genuinely long sentence of content.\nAnother genuine sentence here."


def test_empty_input_stays_empty():
    assert clean_text("") == ""
    assert clean_text("   \n\n  ") == ""


def test_cleaning_is_idempotent():
    raw = "  Some text \u2014 with\n\n\n noise.  \nAccept all cookies\n"
    once = clean_text(raw)
    assert clean_text(once) == once


@given(st.text())
def test_clean_text_never_raises(value):
    assert isinstance(clean_text(value), str)
