"""Shared response primitives."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ItemStatus(StrEnum):
    """Outcome of a single item inside a batch request."""

    OK = "ok"
    ERROR = "error"


class StrictModel(BaseModel):
    """Base model that rejects unknown fields, so typos surface loudly."""

    model_config = ConfigDict(extra="forbid")


class ErrorPayload(StrictModel):
    """Compact error object embedded in a failed batch item."""

    code: str = Field(description="Stable machine-readable error code.")
    title: str = Field(description="Short human-readable summary.")
    detail: str = Field(description="Explanation specific to this occurrence.")


class Usage(StrictModel):
    """Token accounting reported by the LLM provider, when available."""

    input_tokens: int | None = None
    output_tokens: int | None = None
