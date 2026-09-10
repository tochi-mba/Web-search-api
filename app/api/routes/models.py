"""The model catalogue endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api import deps
from app.schemas.models import (
    ModelCapabilitiesOut,
    ModelOut,
    ModelsResponse,
    ProviderOut,
)
from app.services.llm.capabilities import resolve_capabilities
from app.services.llm.registry import ModelRegistry

router = APIRouter(prefix="/v1", tags=["models"])


@router.get("/models", response_model=ModelsResponse, summary="List available models")
async def list_models(
    registry: Annotated[ModelRegistry, Depends(deps.get_registry)],
    refresh: Annotated[
        bool, Query(description="Re-probe providers instead of using the cache.")
    ] = False,
) -> ModelsResponse:
    """List every model that is reachable right now.

    Providers whose credential is wrong or whose endpoint is down are reported
    with their status but contribute no models, so anything listed here can
    actually be used.
    """
    catalog = await registry.catalog(refresh=refresh)

    models = []
    for model in catalog.models:
        capabilities = resolve_capabilities(
            model.model,
            live_context_window=model.context_window,
            live_max_output_tokens=model.max_output_tokens,
        )
        models.append(
            ModelOut(
                id=model.id,
                provider=model.provider,
                model=model.model,
                display_name=model.display_name,
                context_window=capabilities.context_window,
                max_output_tokens=capabilities.max_output_tokens,
                capabilities=ModelCapabilitiesOut(
                    supports_temperature=capabilities.supports_temperature,
                    supports_json_mode=capabilities.supports_json_mode,
                    supports_streaming=capabilities.supports_streaming,
                    reasoning=str(capabilities.reasoning),
                    max_tokens_param=str(capabilities.max_tokens_param),
                ),
            )
        )

    return ModelsResponse(
        default_model=registry.default_model,
        models=models,
        providers=[
            ProviderOut(
                name=health.name,
                status=str(health.status),
                detail=health.detail,
                model_count=health.model_count,
            )
            for health in catalog.providers
        ],
    )
