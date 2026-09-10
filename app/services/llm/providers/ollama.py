"""Ollama provider for locally-served models.

Ollama exposes both a native API and an OpenAI-compatible shim. The native
``/api/tags`` endpoint is used for discovery because it reports richer metadata,
while chat goes through the OpenAI-compatible path so the shared shaping logic
applies unchanged.
"""

from __future__ import annotations

import os

import httpx

from app.core.errors import ProviderUnavailableError, TimeoutProblem, UpstreamError
from app.services.llm.base import ChatRequest, ChatResponse, ModelInfo
from app.services.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.services.llm.specs import AuthStyle, ProviderKind, ProviderSpec

PROVIDER_KEY = "ollama"

DEFAULT_BASE_URL = "http://localhost:11434"

#: Native discovery endpoint; richer than the OpenAI shim's /v1/models.
TAGS_PATH = "/api/tags"


class OllamaProvider:
    """Talks to a local (or LAN) Ollama daemon."""

    name = PROVIDER_KEY

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        """Create the provider.

        Args:
            client: Shared HTTP client.
            base_url: Daemon root. Falls back to ``OLLAMA_BASE_URL``, then localhost.
            timeout_seconds: Per-request timeout. Local models can be slow.
        """
        # ``None`` means "not specified"; an explicit "" disables the provider.
        if base_url is not None:
            resolved = base_url
        else:
            resolved = os.environ.get("OLLAMA_BASE_URL", "").strip() or DEFAULT_BASE_URL
        self._base_url = resolved.rstrip("/")
        self._client = client
        self._timeout = timeout_seconds
        self._chat = OpenAICompatibleProvider(
            ProviderSpec(
                key=PROVIDER_KEY,
                label="Ollama",
                base_url=f"{self._base_url}/v1",
                kind=ProviderKind.LOCAL,
                auth=AuthStyle.NONE,
            ),
            client,
            base_url=f"{self._base_url}/v1",
            timeout_seconds=timeout_seconds,
        )

    def is_configured(self) -> bool:
        """Ollama needs no credential, only a base URL."""
        return bool(self._base_url)

    async def list_models(self) -> list[ModelInfo]:
        """List the models the daemon has pulled locally."""
        try:
            response = await self._client.get(f"{self._base_url}{TAGS_PATH}", timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise TimeoutProblem("Ollama timed out", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("Ollama unreachable", detail=str(exc)) from exc

        if response.status_code != 200:
            raise ProviderUnavailableError(
                "Ollama returned an error",
                detail=f"{self._base_url} responded {response.status_code}.",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("Ollama returned invalid JSON", detail=str(exc)) from exc

        models: list[ModelInfo] = []
        for entry in payload.get("models", []):
            model_name = entry.get("name") or entry.get("model")
            if not model_name:
                continue
            details = entry.get("details") or {}
            models.append(
                ModelInfo.build(
                    self.name,
                    str(model_name),
                    display_name=details.get("family") or None,
                    context_window=entry.get("context_length"),
                )
            )
        return models

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Run one completion through Ollama's OpenAI-compatible endpoint."""
        return await self._chat.chat(request)
