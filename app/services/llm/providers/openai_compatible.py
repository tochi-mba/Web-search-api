"""One adapter for every OpenAI-compatible provider.

Parameterised by a :class:`~app.services.llm.specs.ProviderSpec`, so the ~50
vendors that speak OpenAI's wire format share a single tested implementation.
Per-model differences are handled by the capability layer, not here.
"""

from __future__ import annotations

import os

import httpx

from app.core.errors import (
    ProviderUnavailableError,
    RateLimitedError,
    TimeoutProblem,
    UpstreamError,
)
from app.core.logging import get_logger
from app.services.llm.base import ChatRequest, ChatResponse, ModelInfo
from app.services.llm.capabilities import (
    find_unsupported_parameter,
    resolve_capabilities,
    shape_openai_body,
)
from app.services.llm.specs import AuthStyle, ProviderSpec

logger = get_logger(__name__)


class OpenAICompatibleProvider:
    """Talks to any provider implementing OpenAI's chat completions API."""

    def __init__(
        self,
        spec: ProviderSpec,
        client: httpx.AsyncClient,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Create the provider.

        Args:
            spec: The vendor's table row.
            client: Shared HTTP client.
            api_key: Credential override. Falls back to the spec's env var.
            base_url: Endpoint override. Falls back to the spec's env var, then
                the spec default.
            timeout_seconds: Per-request timeout.
        """
        self.spec = spec
        self.name = spec.key
        self._client = client
        self._timeout = timeout_seconds
        self._api_key = api_key if api_key is not None else self._key_from_env(spec)
        # ``None`` means "not specified"; an explicit "" disables the provider,
        # matching how ``api_key`` is treated just above.
        resolved_base = base_url if base_url is not None else self._base_url_from_env(spec)
        self._base_url = resolved_base.rstrip("/")

    @staticmethod
    def _key_from_env(spec: ProviderSpec) -> str:
        """Read the provider's credential from the environment."""
        if spec.api_key_env is None:
            return ""
        return os.environ.get(spec.api_key_env, "").strip()

    @staticmethod
    def _base_url_from_env(spec: ProviderSpec) -> str:
        """Read the provider's endpoint override, falling back to the default."""
        if spec.base_url_env:
            return os.environ.get(spec.base_url_env, "").strip() or spec.base_url
        return spec.base_url

    def is_configured(self) -> bool:
        """Whether this provider has what it needs to be probed."""
        if not self._base_url:
            return False
        if self.spec.requires_key:
            return bool(self._api_key)
        return True

    def _headers(self) -> dict[str, str]:
        """Build the auth headers for this provider's convention."""
        headers = {"Content-Type": "application/json"}
        if self.spec.auth is AuthStyle.BEARER and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif self.spec.auth is AuthStyle.HEADER and self.spec.auth_header and self._api_key:
            headers[self.spec.auth_header] = self._api_key
        return headers

    def _params(self) -> dict[str, str]:
        """Build query parameters for providers that authenticate that way."""
        if self.spec.auth is AuthStyle.QUERY and self._api_key:
            return {"key": self._api_key}
        return {}

    async def list_models(self) -> list[ModelInfo]:
        """List the models this provider currently offers.

        Providers with no usable list endpoint fall back to the static list in
        their spec, so they still appear in the catalogue.
        """
        if self.spec.static_models:
            return [ModelInfo.build(self.name, model) for model in self.spec.static_models]

        response = await self._get(self.spec.models_path)
        payload = response.json()
        entries = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise UpstreamError(
                "Malformed model list", detail=f"{self.name} returned an unexpected payload."
            )

        models: list[ModelInfo] = []
        for entry in entries:
            model_id = entry.get("id") if isinstance(entry, dict) else None
            if not model_id:
                continue
            models.append(
                ModelInfo.build(
                    self.name,
                    str(model_id),
                    display_name=entry.get("name") or None,
                    context_window=_as_int(
                        entry.get("context_length") or entry.get("context_window")
                    ),
                    max_output_tokens=_as_int(entry.get("max_output_tokens")),
                )
            )
        return models

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Run one completion, shaping the body to what the model accepts."""
        capabilities = resolve_capabilities(request.model)
        shaped = shape_openai_body(
            model=request.model,
            messages=[{"role": m.role, "content": m.content} for m in request.messages],
            system=request.system,
            max_output_tokens=request.max_output_tokens,
            temperature=request.temperature,
            effort=request.effort,
            json_mode=request.json_mode,
            capabilities=capabilities,
        )

        body = shaped.body
        adjustments = list(shaped.adjustments)

        try:
            payload = await self._post_chat(body)
        except UpstreamError as exc:
            # Self-healing: a provider that names an unsupported parameter gets
            # one retry without it, so models released after this code was
            # written degrade to a working request instead of failing.
            offender = find_unsupported_parameter(exc.detail, body)
            if offender is None:
                raise
            retry_body = {k: v for k, v in body.items() if k != offender}
            adjustments.append(f"retried without '{offender}': provider rejected it")
            logger.warning("llm.retry_without_param", provider=self.name, param=offender)
            payload = await self._post_chat(retry_body)

        return self._to_response(request, payload, adjustments)

    def _to_response(
        self, request: ChatRequest, payload: dict[str, object], adjustments: list[str]
    ) -> ChatResponse:
        """Turn a provider payload into a :class:`ChatResponse`."""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise UpstreamError("Empty completion", detail=f"{self.name} returned no choices.")

        first = choices[0]
        if not isinstance(first, dict):
            raise UpstreamError(
                "Malformed completion", detail=f"{self.name} returned an unexpected choice."
            )
        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        text = content if isinstance(content, str) else ""
        usage_raw = payload.get("usage")
        usage: dict[str, object] = usage_raw if isinstance(usage_raw, dict) else {}

        return ChatResponse(
            text=text.strip(),
            model=request.model,
            provider=self.name,
            input_tokens=_as_int(usage.get("prompt_tokens")),
            output_tokens=_as_int(usage.get("completion_tokens")),
            param_adjustments=adjustments,
            finish_reason=_as_str(first.get("finish_reason")),
        )

    async def _get(self, path: str) -> httpx.Response:
        """Issue a GET, translating transport and status failures."""
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                headers=self._headers(),
                params=self._params(),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutProblem(f"{self.name} timed out", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"{self.name} unreachable", detail=str(exc)) from exc

        self._raise_for_status(response)
        return response

    async def _post_chat(self, body: dict[str, object]) -> dict[str, object]:
        """Issue the chat completion request."""
        try:
            response = await self._client.post(
                f"{self._base_url}{self.spec.chat_path}",
                headers=self._headers(),
                params=self._params(),
                json=body,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutProblem(f"{self.name} timed out", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"{self.name} unreachable", detail=str(exc)) from exc

        self._raise_for_status(response)
        try:
            payload: dict[str, object] = response.json()
        except ValueError as exc:
            raise UpstreamError(f"{self.name} returned invalid JSON", detail=str(exc)) from exc
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Map an HTTP status onto the right domain error."""
        if response.status_code < 400:
            return

        detail = _error_detail(response)
        if response.status_code in (401, 403):
            raise ProviderUnavailableError(
                f"{self.name} rejected the credential",
                detail=detail,
            )
        if response.status_code == 429:
            raise RateLimitedError(f"{self.name} rate limited", detail=detail)
        raise UpstreamError(f"{self.name} returned {response.status_code}", detail=detail)


def _error_detail(response: httpx.Response) -> str:
    """Extract the most useful error text a provider gave us."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error is not None:
            return str(error)
        if "message" in payload:
            return str(payload["message"])
    return str(payload)[:500]


def _as_str(value: object) -> str | None:
    """Coerce a provider-supplied value to a string, or ``None``."""
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    """Coerce a provider-supplied number to an int, or ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
