"""Normalisation of scraped text before it reaches a language model.

Scraped pages carry navigation chrome, cookie banners, zero-width characters and
typographic quotes. None of that helps a summariser and all of it costs tokens,
so it is stripped here in one deterministic, well-tested place.
"""

from __future__ import annotations

import re
import unicodedata

#: Characters that carry no meaning but do carry tokens.
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)

#: Typographic characters folded to their ASCII equivalents.
_PUNCTUATION_FOLDING = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2026": "...",
        "\u00a0": " ",
        "\u202f": " ",
        "\u2009": " ",
    }
)

#: Lines matching any of these are page furniture rather than content.
_BOILERPLATE_PATTERNS = (
    r"accept (all )?cookies",
    r"we use cookies",
    r"cookie (policy|preferences|settings|consent)",
    r"skip to (main )?content",
    r"subscribe to our newsletter",
    r"sign (up|in) (to|for)",
    r"enable javascript",
    r"your browser is not supported",
    r"all rights reserved",
    r"privacy policy",
    r"terms of (use|service)",
    r"share (this )?(on|via)",
    r"advertisement",
)

_BOILERPLATE = re.compile("|".join(_BOILERPLATE_PATTERNS), re.IGNORECASE)

#: A line this short with no letters is a separator or stray markup.
_MIN_MEANINGFUL_LINE = 3

_HORIZONTAL_WS = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[^\S\n]+\n")


def collapse_whitespace(text: str) -> str:
    """Collapse redundant whitespace while preserving paragraph structure.

    Runs of spaces and tabs become a single space; three or more consecutive
    newlines become exactly two, so paragraph boundaries survive but vertical
    padding does not.
    """
    text = _HORIZONTAL_WS.sub(" ", text)
    text = _TRAILING_WS.sub("\n", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def _is_noise(line: str) -> bool:
    """Whether a single line is page furniture rather than real content."""
    stripped = line.strip()
    if not stripped:
        return False
    if _BOILERPLATE.search(stripped):
        return True
    return len(stripped) < _MIN_MEANINGFUL_LINE and not any(c.isalnum() for c in stripped)


def clean_text(text: str) -> str:
    """Normalise scraped text into something worth spending tokens on.

    Applies Unicode NFKC normalisation, removes zero-width and control
    characters, folds typographic punctuation to ASCII, drops boilerplate lines
    and collapses whitespace.

    Args:
        text: Raw extracted text.

    Returns:
        Cleaned text, ready to be truncated and sent to a model.
    """
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_ZERO_WIDTH)
    text = text.translate(_PUNCTUATION_FOLDING)
    text = "".join(
        char for char in text if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )

    kept = [line for line in text.split("\n") if not _is_noise(line)]
    return collapse_whitespace("\n".join(kept))
