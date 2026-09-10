"""Request and response models for the search endpoint."""

from __future__ import annotations

from pydantic import Field, field_validator

from app import constants
from app.schemas.common import ErrorPayload, ItemStatus, StrictModel
from app.schemas.summary import SummaryOut


class SearchQueryIn(StrictModel):
    """One query in a batch."""

    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=10, ge=1, le=constants.MAX_RESULTS_PER_QUERY)
    site: str | None = Field(
        default=None, max_length=253, description="Restrict results to this domain."
    )
    additional_notes: str | None = Field(
        default=None,
        max_length=constants.MAX_NOTES_CHARS,
        description="Per-query guidance for the summariser.",
    )

    @field_validator("query")
    @classmethod
    def _query_is_not_blank(cls, value: str) -> str:
        """Reject whitespace-only queries, which no backend can serve."""
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()


class SearchRequest(StrictModel):
    """A batch of search queries plus how to handle them."""

    queries: list[SearchQueryIn] = Field(min_length=1, max_length=constants.MAX_QUERIES_PER_REQUEST)

    fetch_pages: bool = Field(
        default=False,
        description="Also scrape the top result pages instead of using snippets alone.",
    )
    max_pages: int = Field(
        default=3, ge=1, le=10, description="How many result pages to scrape per query."
    )

    summarize: bool = True
    model: str | None = Field(
        default=None, description="Namespaced model id. Defaults to the server's default."
    )
    additional_notes: str | None = Field(
        default=None,
        max_length=constants.MAX_NOTES_CHARS,
        description="Guidance applied to every query in this batch.",
    )

    background: bool = Field(
        default=False,
        alias="async",
        description=(
            "Run in the background: returns 202 with a job id to poll instead "
            "of holding the connection open. Also accepted as 'async'."
        ),
    )

    language: str = Field(default="en", max_length=8)
    region: str = Field(default="us", max_length=8)
    safe_search: bool = True


class SearchResultOut(StrictModel):
    """One organic search result."""

    title: str
    url: str
    snippet: str
    rank: int
    content: str | None = Field(
        default=None, description="Extracted page text, present only when fetch_pages was set."
    )


class SearchQueryResult(StrictModel):
    """The outcome of one query in the batch."""

    query: str
    status: ItemStatus
    backend: str | None = None
    results: list[SearchResultOut] = Field(default_factory=list)
    summary: SummaryOut | None = None
    error: ErrorPayload | None = None


class SearchResponse(StrictModel):
    """The outcome of the whole batch."""

    results: list[SearchQueryResult]
