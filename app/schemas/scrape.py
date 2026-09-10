"""Request and response models for the scrape and summarize endpoints."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from app import constants
from app.schemas.common import ErrorPayload, ItemStatus, StrictModel
from app.schemas.summary import SummaryOut


class RenderMode(StrEnum):
    """How hard to try to render a page before extracting it."""

    AUTO = "auto"
    """Plain HTTP first; fall back to a browser when the result looks empty."""

    ALWAYS = "always"
    """Always render with a headless browser."""

    NEVER = "never"
    """Plain HTTP only."""


class ScrapeRequest(StrictModel):
    """A batch of URLs to fetch, extract and optionally summarise."""

    urls: list[str] = Field(min_length=1, max_length=constants.MAX_URLS_PER_REQUEST)
    render_js: RenderMode = RenderMode.AUTO
    summarize: bool = True
    model: str | None = None
    additional_notes: str | None = Field(default=None, max_length=constants.MAX_NOTES_CHARS)
    summarize_together: bool = Field(
        default=False,
        description="Produce one summary across all URLs instead of one per URL.",
    )


class ExtractedPage(StrictModel):
    """Readable content recovered from one page."""

    url: str
    final_url: str
    title: str | None = None
    author: str | None = None
    published: str | None = None
    text: str
    word_count: int
    rendered: bool = Field(description="Whether a headless browser was used.")


class ScrapeResult(StrictModel):
    """The outcome of scraping one URL."""

    url: str
    status: ItemStatus
    page: ExtractedPage | None = None
    summary: SummaryOut | None = None
    error: ErrorPayload | None = None


class ScrapeResponse(StrictModel):
    """The outcome of the whole batch."""

    results: list[ScrapeResult]
    summary: SummaryOut | None = Field(
        default=None, description="Present when summarize_together was set."
    )


class SummarizeRequest(StrictModel):
    """Summarise text the caller already has."""

    text: str = Field(min_length=1)
    model: str | None = None
    additional_notes: str | None = Field(default=None, max_length=constants.MAX_NOTES_CHARS)
    topic: str | None = Field(default=None, max_length=500)
    sources: list[str] = Field(default_factory=list, max_length=50)


class SummarizeResponse(StrictModel):
    """A summary of caller-supplied text."""

    summary: SummaryOut
