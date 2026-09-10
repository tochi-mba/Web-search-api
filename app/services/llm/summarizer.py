"""Turning cleaned page text into an executive summary.

Handles the two things that make this awkward in practice: fitting the text into
whatever context the chosen model has, and coping with models that ignore the
instruction to answer in JSON.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app import constants
from app.core.logging import get_logger
from app.services.llm.base import ChatMessage, ChatRequest
from app.services.llm.capabilities import resolve_capabilities
from app.services.llm.prompts import build_summary_prompt
from app.services.llm.registry import ModelRegistry
from app.services.text.truncate import char_budget_for_context, truncate

logger = get_logger(__name__)

#: Matches a fenced code block so a JSON answer wrapped in markdown still parses.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

#: Matches the outermost JSON object in a response.
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Summary:
    """A synthesised executive summary and how it was produced."""

    executive_summary: str
    key_points: list[str] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    truncated: bool = False
    chars_submitted: int = 0
    original_chars: int = 0
    notes_applied: bool = False
    param_adjustments: list[str] = field(default_factory=list)


def parse_summary_payload(text: str) -> tuple[str, list[str]]:
    """Recover a summary from a model response, tolerating sloppy formatting.

    Models are asked for JSON, and most comply. Some wrap it in a markdown
    fence, some prepend a sentence, and some ignore the instruction entirely.
    All three are handled rather than surfaced as an error, because a slightly
    messy summary is far more useful to a caller than a 502.

    Args:
        text: The raw model response.

    Returns:
        ``(executive_summary, key_points)``.
    """
    stripped = text.strip()
    if not stripped:
        return "", []

    fenced = _FENCE.search(stripped)
    candidate = fenced.group(1).strip() if fenced else stripped

    for source in (candidate, stripped):
        parsed = _try_parse_object(source)
        if parsed is not None:
            return parsed

    # No usable JSON: treat the whole response as the summary prose.
    return stripped, []


def _try_parse_object(source: str) -> tuple[str, list[str]] | None:
    """Attempt to read a summary object out of ``source``."""
    for text in (source, _first_object(source)):
        if not text:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue

        summary = payload.get("executive_summary") or payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            continue

        raw_points = payload.get("key_points") or []
        points = (
            [str(point).strip() for point in raw_points if str(point).strip()]
            if isinstance(raw_points, list)
            else []
        )
        return summary.strip(), points
    return None


def _first_object(source: str) -> str:
    """Extract the outermost JSON object from a string, if there is one."""
    match = _JSON_OBJECT.search(source)
    return match.group(0) if match else ""


class Summarizer:
    """Produces executive summaries with whichever model the caller chose."""

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        max_content_chars: int = constants.MAX_CONTENT_CHARS,
        max_output_tokens: int = 2_000,
    ) -> None:
        """Create the summariser.

        Args:
            registry: Used to resolve the model and reach its provider.
            max_content_chars: Hard ceiling on submitted characters.
            max_output_tokens: Requested length of the summary itself.
        """
        self._registry = registry
        self._max_content_chars = max_content_chars
        self._max_output_tokens = max_output_tokens

    async def summarize(
        self,
        content: str,
        *,
        model_id: str | None = None,
        topic: str | None = None,
        additional_notes: str | None = None,
        sources: list[str] | None = None,
    ) -> Summary:
        """Summarise ``content`` with the requested model.

        The character budget is the smaller of the configured ceiling and what
        the resolved model's context window can hold, so a 200K-context model
        truncates harder than a 1M one without the caller doing anything.
        """
        model = await self._registry.resolve(model_id)
        capabilities = resolve_capabilities(
            model.model,
            live_context_window=model.context_window,
            live_max_output_tokens=model.max_output_tokens,
        )

        budget = char_budget_for_context(
            context_window=capabilities.context_window,
            hard_limit=self._max_content_chars,
        )
        bounded = truncate(content, limit=budget)

        prompt = build_summary_prompt(
            bounded.text,
            topic=topic,
            additional_notes=additional_notes,
            sources=sources,
        )

        if not bounded.text.strip():
            # Nothing was scraped worth summarising. Spending a model call to be
            # told so would be waste.
            return Summary(
                executive_summary="",
                model=model.id,
                provider=model.provider,
                truncated=bounded.truncated,
                chars_submitted=0,
                original_chars=bounded.original_chars,
                notes_applied=prompt.notes_applied,
            )

        response = await self._registry.complete(
            ChatRequest(
                model=model.model,
                messages=[ChatMessage(role="user", content=prompt.user)],
                system=prompt.system,
                max_output_tokens=self._max_output_tokens,
                json_mode=True,
            ),
            model_id=model.id,
        )

        executive_summary, key_points = parse_summary_payload(response.text)
        logger.info(
            "summary.produced",
            model=model.id,
            chars_submitted=bounded.chars_submitted,
            truncated=bounded.truncated,
            key_points=len(key_points),
        )

        return Summary(
            executive_summary=executive_summary,
            key_points=key_points,
            model=model.id,
            provider=model.provider,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            truncated=bounded.truncated,
            chars_submitted=bounded.chars_submitted,
            original_chars=bounded.original_chars,
            notes_applied=prompt.notes_applied,
            param_adjustments=response.param_adjustments,
        )
