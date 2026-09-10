"""Anthropic provider, using the official SDK.

Anthropic's Messages API is not OpenAI-shaped - system prompts are a top-level
field, thinking configuration differs by model generation, and a request can
come back with ``stop_reason == "refusal"`` on a 200 - so it gets a dedicated
adapter rather than riding the compatible one.
"""

from __future__ import annotations

from typing import Any

import anthropic

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
    ReasoningStyle,
    clamp_output_tokens,
    resolve_capabilities,
)

logger = get_logger(__name__)

PROVIDER_KEY = "anthropic"

#: Fraction of the output budget given to thinking on budget_tokens models.
_BUDGET_FRACTION = 0.5
_MIN_BUDGET_TOKENS = 1_024


class AnthropicProvider:
    """Talks to the Anthropic Messages API."""

    name = PROVIDER_KEY

    #: Anthropic needs a credential; there is no keyless mode.
    requires_credential = True

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        """Create the provider.

        No credential is held here. Anthropic's SDK wants its key at client
        construction, so a client is built per call from whatever keyring
        resolved for that caller.

        Args:
            base_url: Endpoint override, e.g. a gateway or proxy.
            timeout_seconds: Per-request timeout.
            max_retries: How many times the SDK retries transient failures.
        """
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    def is_configured(self) -> bool:
        """Always true: the endpoint is known and the credential is per caller."""
        return True

    def client_for(self, auth: ResolvedAuth) -> anthropic.AsyncAnthropic:
        """Build an SDK client carrying this caller's credential.

        The resolved headers are passed through as default headers rather than
        unpacked into ``api_key``: keyring decides which header its key belongs
        on, and second-guessing that here would break the moment somebody stores
        a credential this code did not anticipate.
        """
        kwargs: dict[str, Any] = {
            # The SDK requires the argument; the real credential travels in the
            # default headers keyring gave us, which take precedence on the wire.
            "api_key": "unused-credential-comes-from-keyring",
            "timeout": self._timeout,
            "max_retries": self._max_retries,
            "default_headers": dict(auth.headers),
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return anthropic.AsyncAnthropic(**kwargs)

    async def list_models(self, auth: ResolvedAuth) -> list[ModelInfo]:
        """List every Claude model this caller's key can reach."""
        client = self.client_for(auth)
        try:
            page = await client.models.list(limit=100)
        except anthropic.AuthenticationError as exc:
            raise ProviderUnavailableError(
                "Anthropic rejected the credential", detail=str(exc)
            ) from exc
        except anthropic.RateLimitError as exc:
            raise RateLimitedError("Anthropic rate limited", detail=str(exc)) from exc
        except anthropic.APITimeoutError as exc:
            raise TimeoutProblem("Anthropic timed out", detail=str(exc)) from exc
        except anthropic.APIError as exc:
            raise ProviderUnavailableError("Anthropic unreachable", detail=str(exc)) from exc
        finally:
            await client.close()

        models: list[ModelInfo] = []
        for entry in page.data:
            capabilities = resolve_capabilities(entry.id)
            models.append(
                ModelInfo.build(
                    self.name,
                    entry.id,
                    display_name=getattr(entry, "display_name", None),
                    context_window=getattr(entry, "max_input_tokens", None)
                    or capabilities.context_window,
                    max_output_tokens=getattr(entry, "max_tokens", None)
                    or capabilities.max_output_tokens,
                )
            )
        return models

    def _build_kwargs(self, request: ChatRequest) -> tuple[dict[str, Any], list[str]]:
        """Shape a Messages API call for the specific model being addressed."""
        capabilities = resolve_capabilities(request.model)
        adjustments: list[str] = []

        tokens, note = clamp_output_tokens(request.max_output_tokens, capabilities)
        if note:
            adjustments.append(note)

        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": tokens,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.system:
            kwargs["system"] = request.system

        if request.temperature is not None:
            if capabilities.supports_temperature:
                # Current SDK versions removed sampling parameters from the
                # typed signature because the newest models reject them. Models
                # that still accept temperature get it through extra_body so the
                # intent survives without fighting the SDK's types.
                kwargs["extra_body"] = {"temperature": request.temperature}
            else:
                adjustments.append("temperature dropped: rejected by this model")

        if request.effort is not None:
            if capabilities.reasoning is ReasoningStyle.ADAPTIVE_THINKING:
                kwargs["thinking"] = {"type": "adaptive"}
                kwargs["output_config"] = {"effort": request.effort}
            elif capabilities.reasoning is ReasoningStyle.BUDGET_TOKENS:
                budget = max(int(tokens * _BUDGET_FRACTION), _MIN_BUDGET_TOKENS)
                if budget < tokens:
                    kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
                    adjustments.append(
                        f"effort '{request.effort}' translated to budget_tokens={budget}"
                    )
                else:
                    adjustments.append(
                        "thinking disabled: output budget too small for a thinking budget"
                    )
            else:
                adjustments.append("effort dropped: this model has no reasoning controls")

        return kwargs, adjustments

    async def chat(self, request: ChatRequest, auth: ResolvedAuth) -> ChatResponse:
        """Run one completion against the Messages API."""
        kwargs, adjustments = self._build_kwargs(request)
        client = self.client_for(auth)

        try:
            message = await client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise ProviderUnavailableError(
                "Anthropic rejected the credential", detail=str(exc)
            ) from exc
        except anthropic.RateLimitError as exc:
            raise RateLimitedError("Anthropic rate limited", detail=str(exc)) from exc
        except anthropic.APITimeoutError as exc:
            raise TimeoutProblem("Anthropic timed out", detail=str(exc)) from exc
        except anthropic.APIError as exc:
            raise UpstreamError("Anthropic request failed", detail=str(exc)) from exc
        finally:
            await client.close()

        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason == "refusal":
            # A refusal arrives as a 200 with no usable content, so it must be
            # checked explicitly rather than assumed away.
            raise UpstreamError(
                "Anthropic declined the request",
                detail="The model returned stop_reason='refusal' for this content.",
            )

        text = "".join(
            block.text
            for block in message.content
            if getattr(block, "type", None) == "text" and hasattr(block, "text")
        ).strip()

        usage = getattr(message, "usage", None)
        return ChatResponse(
            text=text,
            model=request.model,
            provider=self.name,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            param_adjustments=adjustments,
            finish_reason=stop_reason,
        )
