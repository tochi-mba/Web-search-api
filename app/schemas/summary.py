"""Shared summary response model."""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import StrictModel, Usage


class SummaryOut(StrictModel):
    """An LLM-synthesised executive summary and how it was produced."""

    executive_summary: str
    key_points: list[str] = Field(default_factory=list)
    model: str
    provider: str
    usage: Usage = Field(default_factory=Usage)
    truncated: bool = Field(
        description="Whether the source text was cut to fit the model's budget."
    )
    chars_submitted: int = Field(description="Characters actually sent to the model.")
    original_chars: int = Field(description="Characters available before truncation.")
    notes_applied: bool = Field(
        description="Whether caller-supplied additional_notes reached the prompt."
    )
    param_adjustments: list[str] = Field(
        default_factory=list,
        description="Parameters dropped, renamed or translated for this model.",
    )
