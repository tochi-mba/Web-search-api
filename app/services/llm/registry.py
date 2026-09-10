"""The model registry: probing, catalogue assembly and model resolution.

The contract that matters here is the one the caller sees: ``GET /v1/models``
lists only models that are **reachable right now**. A provider with a wrong key
or a down endpoint drops out of the catalogue instead of failing at request
time, which is the whole point of probing rather than trusting configuration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.core.cache import TTLCache
from app.core.concurrency import bounded_gather
from app.core.errors import (
    NotFoundError,
    ProviderUnavailableError,
    RateLimitedError,
    ValidationProblem,
)
from app.core.logging import get_logger
from app.services.llm.base import (
    ChatRequest,
    ChatResponse,
    LLMProvider,
    ModelInfo,
    ProviderHealth,
    ProviderStatus,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """Everything the service currently knows about available models."""

    models: list[ModelInfo] = field(default_factory=list)
    providers: list[ProviderHealth] = field(default_factory=list)

    def find(self, model_id: str) -> ModelInfo | None:
        """Look up one model by its namespaced id."""
        return next((m for m in self.models if m.id == model_id), None)


def split_model_id(model_id: str) -> tuple[str, str]:
    """Split ``provider:model`` into its parts.

    Splits on the **first** colon only, because Ollama tags legitimately contain
    colons - ``ollama:llama3.1:8b`` names the model ``llama3.1:8b``.

    Raises:
        ValidationProblem: The id is not namespaced.
    """
    provider, separator, model = model_id.partition(":")
    if not separator or not provider or not model:
        raise ValidationProblem(
            "Malformed model id",
            detail=f"'{model_id}' must be namespaced as 'provider:model'.",
        )
    return provider, model


class ModelRegistry:
    """Probes providers, caches the result and resolves models to providers."""

    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        default_model: str,
        cache_ttl_seconds: float,
        probe_timeout_seconds: float = 5.0,
        max_concurrency: int = 8,
    ) -> None:
        """Create the registry.

        Args:
            providers: Every provider this deployment knows about.
            default_model: Namespaced id used when a caller does not choose.
            cache_ttl_seconds: How long a probed catalogue stays fresh.
            probe_timeout_seconds: Per-provider probe deadline.
            max_concurrency: How many providers to probe at once.
        """
        self._providers = {provider.name: provider for provider in providers}
        self._default_model = default_model
        self._probe_timeout = probe_timeout_seconds
        self._max_concurrency = max_concurrency
        self._cache: TTLCache[ModelCatalog] = TTLCache(cache_ttl_seconds)

    @property
    def default_model(self) -> str:
        """The configured default model id."""
        return self._default_model

    @property
    def provider_names(self) -> list[str]:
        """Keys of every registered provider."""
        return list(self._providers)

    async def catalog(self, *, refresh: bool = False) -> ModelCatalog:
        """Return the current catalogue, probing providers when stale."""
        if refresh:
            self._cache.invalidate()
        return await self._cache.get(self._probe_all)

    async def _probe_all(self) -> ModelCatalog:
        """Probe every provider concurrently and assemble the catalogue."""
        names = list(self._providers)
        results = await bounded_gather(
            [self._make_probe(name) for name in names],
            limit=self._max_concurrency,
        )

        models: list[ModelInfo] = []
        healths: list[ProviderHealth] = []
        for health, provider_models in results:
            healths.append(health)
            if health.is_available:
                models.extend(provider_models)

        models.sort(key=lambda m: m.id)
        healths.sort(key=lambda h: h.name)
        logger.info(
            "models.probed",
            available=sum(1 for h in healths if h.is_available),
            total=len(healths),
            models=len(models),
        )
        return ModelCatalog(models=models, providers=healths)

    def _make_probe(self, name: str):  # type: ignore[no-untyped-def] # noqa: ANN202
        """Build a zero-argument coroutine factory probing one provider."""

        async def probe() -> tuple[ProviderHealth, list[ModelInfo]]:
            return await self._probe_one(name)

        return probe

    async def _probe_one(self, name: str) -> tuple[ProviderHealth, list[ModelInfo]]:
        """Probe a single provider and classify the outcome."""
        provider = self._providers[name]

        if not provider.is_configured():
            return (
                ProviderHealth(
                    name=name,
                    status=ProviderStatus.NOT_CONFIGURED,
                    detail="No credential or endpoint configured.",
                ),
                [],
            )

        try:
            models = await asyncio.wait_for(provider.list_models(), timeout=self._probe_timeout)
        except ProviderUnavailableError as exc:
            # A rejected credential and an unreachable host are different
            # problems for whoever has to fix them, so they are reported apart.
            status = (
                ProviderStatus.UNAUTHORIZED
                if "credential" in exc.detail.lower() or "credential" in exc.title.lower()
                else ProviderStatus.UNREACHABLE
            )
            return ProviderHealth(name=name, status=status, detail=exc.detail[:200]), []
        except RateLimitedError as exc:
            return (
                ProviderHealth(
                    name=name, status=ProviderStatus.UNREACHABLE, detail=exc.detail[:200]
                ),
                [],
            )
        except TimeoutError:
            return (
                ProviderHealth(
                    name=name,
                    status=ProviderStatus.UNREACHABLE,
                    detail=f"Did not respond within {self._probe_timeout}s.",
                ),
                [],
            )
        except Exception as exc:
            logger.warning("models.probe_failed", provider=name, error=str(exc))
            return (
                ProviderHealth(name=name, status=ProviderStatus.UNREACHABLE, detail=str(exc)[:200]),
                [],
            )

        return (
            ProviderHealth(
                name=name,
                status=ProviderStatus.AVAILABLE,
                detail=f"{len(models)} models",
                model_count=len(models),
            ),
            models,
        )

    async def resolve(self, model_id: str | None) -> ModelInfo:
        """Resolve a requested model id to something actually available.

        Args:
            model_id: A namespaced id, or ``None`` to use the default.

        Returns:
            The resolved model.

        Raises:
            NotFoundError: The named model is not currently available.
            ProviderUnavailableError: Nothing at all is available.
        """
        catalog = await self.catalog()

        if not catalog.models:
            raise ProviderUnavailableError(
                "No LLM provider is currently available",
                detail=(
                    "Every configured provider failed its health probe. "
                    "Check GET /v1/models for per-provider status."
                ),
            )

        requested = model_id or self._default_model
        split_model_id(requested)

        found = catalog.find(requested)
        if found is not None:
            return found

        if model_id is not None:
            available = ", ".join(m.id for m in catalog.models[:10])
            raise NotFoundError(
                "Unknown or unavailable model",
                detail=f"'{model_id}' is not available. Currently available: {available}",
            )

        # The configured default is unreachable; fall back rather than fail.
        fallback = catalog.models[0]
        logger.warning(
            "models.default_unavailable",
            configured=self._default_model,
            fallback=fallback.id,
        )
        return fallback

    def provider_for(self, model: ModelInfo) -> LLMProvider:
        """Return the provider serving ``model``."""
        provider = self._providers.get(model.provider)
        if provider is None:  # pragma: no cover - catalogue entries always map back
            raise NotFoundError(
                "Unknown provider", detail=f"No provider registered for '{model.provider}'."
            )
        return provider

    async def complete(self, request: ChatRequest, *, model_id: str | None = None) -> ChatResponse:
        """Resolve a model and run one completion against its provider."""
        model = await self.resolve(model_id)
        provider = self.provider_for(model)
        return await provider.chat(
            ChatRequest(
                model=model.model,
                messages=request.messages,
                system=request.system,
                max_output_tokens=request.max_output_tokens,
                temperature=request.temperature,
                effort=request.effort,
                json_mode=request.json_mode,
            )
        )
