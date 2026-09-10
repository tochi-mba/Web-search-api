"""One adapter for every OpenAI-compatible provider.

Parameterised by a :class:`~app.services.llm.specs.ProviderSpec`, so the ~50
vendors that speak OpenAI's wire format share a single tested implementation.
Per-model differences are handled by the capability layer, not here, and
credentials arrive per call from keyring rather than being held here at all.
"""

from __future__ import annotations

import httpx

from app.core.errors import (
    ProviderUnavailableError,
    RateLimitedError,
    TimeoutProblem,
    UpstreamError,
)
from app.core.logging import get_logger
from app.services.keyring.client import ResolvedAuth
from app.services.llm.base import ChatRequest, ChatResponse, ModelInfo
from app.services.llm.capabilities import (
    find_unsupported_parameter,
    resolve_capabilities,
    shape_openai_body,
)
from app.services.llm.specs import ProviderSpec

logger = get_logger(__name__)


class OpenAICompatibleProvider:
    """Talks to any provider implementing OpenAI's chat completions API."""

    def __init__(
        self,
        spec: ProviderSpec,
        client: httpx.AsyncClient,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Create the provider.

        Args:
            spec: The vendor's table row.
            client: Shared HTTP client.
            base_url: Endpoint override. ``None`` means "not specified, use the
                spec default"; an explicit "" disables the provider.
            timeout_seconds: Per-request timeout.
        """
        self.spec = spec
        self.name = spec.key
        self.requires_credential = spec.requires_key
        self._client = client
        self._timeout = timeout_seconds
        self._base_url = (base_url if base_url is not None else spec.base_url).rstrip("/")

    def is_configured(self) -> bool:
        """Whether this provider has the non-secret configuration it needs.

        Credentials are not consulted here: they belong to the caller and are
        resolved per request.
        """
        return bool(self._base_url)

    def _headers(self, auth: ResolvedAuth) -> dict[str, str]:
        """Base headers with the caller's credential attached."""
        return {"Content-Type": "application/json", **auth.headers}

    def _params(self, auth: ResolvedAuth) -> dict[str, str]:
        """Query parameters, for providers whose credential goes there."""
        return dict(auth.query_params)

    async def list_models(self, auth: ResolvedAuth) -> list[ModelInfo]:
        """List the models this provider currently offers.

        Providers with no usable list endpoint fall back to the static list in
        their spec, so they still appear in the catalogue.
        """
        if self.spec.static_models:
            return [ModelInfo.build(self.name, model) for model in self.spec.static_models]

        response = await self._get(self.spec.models_path, auth)
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

    async def chat(self, request: ChatRequest, auth: ResolvedAuth) -> ChatResponse:
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
            payload = await self._post_chat(body, auth)
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
            payload = await self._post_chat(retry_body, auth)

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

    async def _get(self, path: str, auth: ResolvedAuth) -> httpx.Response:
        """Issue a GET, translating transport and status failures."""
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                headers=self._headers(auth),
                params=self._params(auth),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutProblem(f"{self.name} timed out", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"{self.name} unreachable", detail=str(exc)) from exc

        self._raise_for_status(response)
        return response

    async def _post_chat(self, body: dict[str, object], auth: ResolvedAuth) -> dict[str, object]:
        """Issue the chat completion request."""
        try:
            response = await self._client.post(
                f"{self._base_url}{self.spec.chat_path}",
                headers=self._headers(auth),
                params=self._params(auth),
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
