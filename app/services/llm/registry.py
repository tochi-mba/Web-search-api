"""The model registry: probing, catalogue assembly and model resolution.

The contract that matters here is the one the caller sees: ``GET /v1/models``
lists only models that are **reachable right now, by you**. A provider the
caller has not connected in keyring, or whose credential no longer works, drops
out of the catalogue instead of failing at request time.

Catalogues are per caller, because credentials are. Two people hitting this
service see different answers, and one must never see the other's - which is
why the cache is keyed by verified account id rather than by anything the
caller supplies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.cache import TTLCache
from app.core.concurrency import bounded_gather
from app.core.errors import (
    DomainError,
    NotFoundError,
    ProviderUnavailableError,
    RateLimitedError,
    ValidationProblem,
)
from app.core.logging import get_logger
from app.services.keyring.caller import Caller
from app.services.keyring.client import NO_AUTH, KeyringClient, NotConnectedError, ResolvedAuth
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
        keyring: KeyringClient | None = None,
        probe_timeout_seconds: float = 5.0,
        max_concurrency: int = 8,
        max_cached_callers: int = 256,
    ) -> None:
        """Create the registry.

        Args:
            providers: Every provider this deployment knows about.
            default_model: Namespaced id used when a caller does not choose.
            cache_ttl_seconds: How long a probed catalogue stays fresh.
            keyring: Where credentials come from. Without one, only providers
                needing no credential are usable.
            probe_timeout_seconds: Per-provider probe deadline.
            max_concurrency: How many providers to probe at once.
            max_cached_callers: How many callers' catalogues to keep. Bounded so
                a busy deployment cannot grow without limit.
        """
        self._providers = {provider.name: provider for provider in providers}
        self._default_model = default_model
        self._keyring = keyring
        self._probe_timeout = probe_timeout_seconds
        self._max_concurrency = max_concurrency
        self._cache_ttl = cache_ttl_seconds
        self._max_cached_callers = max_cached_callers
        self._caches: dict[tuple[str, str], TTLCache[ModelCatalog]] = {}

    #: Cache key used when there is no caller: only keyless providers are visible.
    _ANONYMOUS = ("", "")

    def _cache_for(self, caller: Caller | None) -> TTLCache[ModelCatalog]:
        """Return this caller's catalogue cache, creating it if needed."""
        key = caller.cache_key if caller is not None else self._ANONYMOUS
        cache = self._caches.get(key)
        if cache is None:
            if len(self._caches) >= self._max_cached_callers:
                # Oldest insertion first: dicts preserve insertion order, and a
                # crude bound beats an unbounded map of strangers' catalogues.
                self._caches.pop(next(iter(self._caches)))
            cache = TTLCache(self._cache_ttl)
            self._caches[key] = cache
        return cache

    @property
    def default_model(self) -> str:
        """The configured default model id."""
        return self._default_model

    @property
    def provider_names(self) -> list[str]:
        """Keys of every registered provider."""
        return list(self._providers)

    async def catalog(self, caller: Caller | None = None, *, refresh: bool = False) -> ModelCatalog:
        """Return the catalogue for one caller, probing providers when stale."""
        cache = self._cache_for(caller)
        if refresh:
            cache.invalidate()

        async def probe() -> ModelCatalog:
            return await self._probe_all(caller)

        return await cache.get(probe)

    async def _probe_all(self, caller: Caller | None) -> ModelCatalog:
        """Probe every provider concurrently and assemble the catalogue."""
        names = list(self._providers)
        results = await bounded_gather(
            [self._make_probe(name, caller) for name in names],
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

    def _make_probe(
        self, name: str, caller: Caller | None
    ) -> Callable[[], Awaitable[tuple[ProviderHealth, list[ModelInfo]]]]:
        """Build a zero-argument coroutine factory probing one provider."""

        async def probe() -> tuple[ProviderHealth, list[ModelInfo]]:
            return await self._probe_one(name, caller)

        return probe

    async def credential_for(self, provider: LLMProvider, caller: Caller | None) -> ResolvedAuth:
        """Resolve what to attach for one provider and caller.

        Raises:
            NotConnectedError: This caller has no credential for this provider.
            DomainError: Keyring rejected the caller or could not be reached.
        """
        if not provider.requires_credential:
            return NO_AUTH
        if self._keyring is None or not self._keyring.is_configured or caller is None:
            raise NotConnectedError(provider.name)
        return await self._keyring.resolve(
            profile=caller.profile, service=provider.name, user_token=caller.user_token
        )

    async def _probe_one(
        self, name: str, caller: Caller | None
    ) -> tuple[ProviderHealth, list[ModelInfo]]:
        """Probe a single provider and classify the outcome."""
        provider = self._providers[name]

        if not provider.is_configured():
            return (
                ProviderHealth(
                    name=name,
                    status=ProviderStatus.NOT_CONFIGURED,
                    detail="No endpoint configured.",
                ),
                [],
            )

        try:
            auth = await self.credential_for(provider, caller)
        except NotConnectedError:
            # The common case across fifty providers: this account simply has
            # not connected this one. Short-circuits without touching the
            # provider, which is what keeps a full sweep cheap.
            return (
                ProviderHealth(
                    name=name,
                    status=ProviderStatus.NOT_CONFIGURED,
                    detail="No credential for this caller in keyring.",
                ),
                [],
            )
        except DomainError as exc:
            return (
                ProviderHealth(
                    name=name, status=ProviderStatus.UNAUTHORIZED, detail=exc.detail[:200]
                ),
                [],
            )

        try:
            models = await asyncio.wait_for(provider.list_models(auth), timeout=self._probe_timeout)
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

    async def resolve(self, model_id: str | None, caller: Caller | None = None) -> ModelInfo:
        """Resolve a requested model id to something actually available.

        Args:
            model_id: A namespaced id, or ``None`` to use the default.
            caller: Who the request is for; decides which models are available.

        Returns:
            The resolved model.

        Raises:
            NotFoundError: The named model is not currently available.
            ProviderUnavailableError: Nothing at all is available.
        """
        catalog = await self.catalog(caller)

        if not catalog.models:
            raise ProviderUnavailableError(
                "No LLM provider is currently available",
                detail=(
                    "No provider is reachable for this caller. Check GET /v1/models "
                    "for per-provider status, and that a credential is stored in "
                    "keyring for the profile you named."
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

    async def complete(
        self,
        request: ChatRequest,
        *,
        model_id: str | None = None,
        caller: Caller | None = None,
        auth: ResolvedAuth | None = None,
    ) -> ChatResponse:
        """Resolve a model and run one completion against its provider.

        Args:
            request: What to ask for.
            model_id: Namespaced model id, or ``None`` for the default.
            caller: Who this is for. Used to resolve a credential when one is
                not already in hand.
            auth: A credential resolved earlier - background jobs resolve at
                submit time, while the caller's token is still fresh, and carry
                the result rather than the token.
        """
        model = await self.resolve(model_id, caller)
        provider = self.provider_for(model)

        if auth is None:
            try:
                auth = await self.credential_for(provider, caller)
            except NotConnectedError as exc:
                raise NotFoundError(
                    "No credential for this model",
                    detail=(
                        f"'{model.id}' needs a credential stored in keyring under "
                        f"service '{provider.name}' for this profile."
                    ),
                ) from exc

        return await provider.chat(
            ChatRequest(
                model=model.model,
                messages=request.messages,
                system=request.system,
                max_output_tokens=request.max_output_tokens,
                temperature=request.temperature,
                effort=request.effort,
                json_mode=request.json_mode,
            ),
            auth,
        )
