"""Threshold truncation for text on its way to a language model.

Truncation is always reported back to the caller: silently dropping half a
document and presenting the resulting summary as complete would be a lie, so
:class:`Truncation` carries both what was sent and what was originally there.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import constants

#: How far back from the cut point we will look for a word boundary. Expressed
#: as a fraction of the limit so it scales with the budget.
_BOUNDARY_SEARCH_FRACTION = 0.2


@dataclass(frozen=True, slots=True)
class Truncation:
    """The outcome of applying a character budget to a piece of text."""

    text: str
    truncated: bool
    original_chars: int

    @property
    def chars_submitted(self) -> int:
        """Number of characters actually handed to the model."""
        return len(self.text)


def truncate(text: str, *, limit: int) -> Truncation:
    """Cut ``text`` down to ``limit`` characters, preferring a word boundary.

    Args:
        text: The text to bound.
        limit: Maximum number of characters to keep. Zero or negative keeps none.

    Returns:
        A :class:`Truncation` describing what was kept and whether anything was lost.
    """
    original_chars = len(text)

    if limit <= 0:
        return Truncation(text="", truncated=True, original_chars=original_chars)

    if original_chars <= limit:
        return Truncation(text=text, truncated=False, original_chars=original_chars)

    window = text[:limit]
    boundary = window.rfind(" ")
    newline = window.rfind("\n")
    boundary = max(boundary, newline)

    # Only honour the boundary if it does not throw away most of the budget.
    minimum_keep = int(limit * (1 - _BOUNDARY_SEARCH_FRACTION))
    cut = window[:boundary] if boundary >= minimum_keep else window

    return Truncation(text=cut.rstrip(), truncated=True, original_chars=original_chars)


def char_budget_for_context(*, context_window: int | None, hard_limit: int) -> int:
    """Compute how many characters may be sent to a model.

    The effective budget is the smaller of the configured hard limit and what
    the model's context window can hold once room is reserved for the prompt
    scaffolding and the model's own answer. A 200K-context model therefore
    truncates harder than a 1M-context one, automatically.

    Args:
        context_window: The model's input window in tokens, or ``None`` if unknown.
        hard_limit: The configured ceiling in characters.

    Returns:
        A non-negative character budget.
    """
    if context_window is None:
        return hard_limit

    usable_tokens = (
        context_window - constants.RESERVED_OUTPUT_TOKENS - constants.PROMPT_OVERHEAD_TOKENS
    )
    if usable_tokens <= 0:
        return 0
    return min(hard_limit, usable_tokens * constants.CHARS_PER_TOKEN)
