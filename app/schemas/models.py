"""Response models for the model catalogue endpoint."""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import StrictModel


class ModelCapabilitiesOut(StrictModel):
    """What a model will accept, so callers need not discover it by trial."""

    supports_temperature: bool
    supports_json_mode: bool
    supports_streaming: bool
    reasoning: str = Field(description="How reasoning depth is expressed, or 'none'.")
    max_tokens_param: str


class ModelOut(StrictModel):
    """One available model."""

    id: str = Field(description="Namespaced identifier, e.g. 'anthropic:claude-opus-5'.")
    provider: str
    model: str = Field(description="The bare model id as the provider knows it.")
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    capabilities: ModelCapabilitiesOut


class ProviderOut(StrictModel):
    """The health of one provider."""

    name: str
    status: str = Field(
        description="available, unauthorized, unreachable or not_configured.",
    )
    detail: str = ""
    model_count: int = 0


class ModelsResponse(StrictModel):
    """Everything currently reachable, plus why anything missing is missing."""

    default_model: str
    models: list[ModelOut]
    providers: list[ProviderOut]
