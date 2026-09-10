"""Conversions from internal dataclasses to API response models."""

from __future__ import annotations

from app.schemas.common import Usage
from app.schemas.summary import SummaryOut
from app.services.llm.summarizer import Summary


def to_summary_out(summary: Summary) -> SummaryOut:
    """Render an internal :class:`Summary` as its API representation."""
    return SummaryOut(
        executive_summary=summary.executive_summary,
        key_points=summary.key_points,
        model=summary.model,
        provider=summary.provider,
        usage=Usage(
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
        ),
        truncated=summary.truncated,
        chars_submitted=summary.chars_submitted,
        original_chars=summary.original_chars,
        notes_applied=summary.notes_applied,
        param_adjustments=summary.param_adjustments,
    )
