"""Fixtures for endpoint tests: a real app with fake services injected."""

from __future__ import annotations

import httpx
import pytest

from app.api import deps
from app.main import create_app
from app.schemas.health import ReadinessComponent
from app.services.fetch.page import FetchedPage
from app.services.jobs.memory import InMemoryJobStore
from app.services.jobs.runner import JobRunner
from app.services.llm.registry import ModelRegistry
from app.services.llm.summarizer import Summary
from app.services.search.base import SearchQuery, SearchResponse, SearchResult
from app.services.text.extractor import ExtractedContent
from tests.conftest import make_settings


class FakeSummarizer:
    """Records what it was asked to summarise and returns a canned summary."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None

    async def summarize(
        self,
        content,
        *,
        model_id=None,
        topic=None,
        additional_notes=None,
        sources=None,
        caller=None,
        auth=None,
    ):
        self.calls.append(
            {
                "content": content,
                "model_id": model_id,
                "topic": topic,
                "additional_notes": additional_notes,
                "sources": sources,
                "caller": caller,
                "auth": auth,
            }
        )
        if self.error:
            raise self.error
        return Summary(
            executive_summary="A tight executive summary.",
            key_points=["first point", "second point"],
            model=model_id or "anthropic:claude-opus-5",
            provider="anthropic",
            input_tokens=400,
            output_tokens=50,
            truncated=False,
            chars_submitted=len(content),
            original_chars=len(content),
            notes_applied=bool(additional_notes),
        )


class FakePageFetcher:
    """Serves canned pages, or raises whatever was registered for a URL."""

    def __init__(self) -> None:
        self.pages: dict[str, str] = {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, str]] = []

    def add(self, url: str, text: str, *, title: str = "A Page") -> None:
        self.pages[url] = text
        self._titles = getattr(self, "_titles", {})
        self._titles[url] = title

    def fail(self, url: str, error: Exception) -> None:
        self.errors[url] = error

    async def fetch(self, url: str, *, render: str = "auto") -> FetchedPage:
        self.calls.append((url, render))
        if url in self.errors:
            raise self.errors[url]
        text = self.pages.get(url, "Default page content for testing purposes.")
        titles = getattr(self, "_titles", {})
        return FetchedPage(
            content=ExtractedContent(
                url=url,
                title=titles.get(url, "A Page"),
                author="Someone",
                published="2026-01-01",
                text=text,
            ),
            final_url=url,
            rendered=False,
        )


class FakeSearchRouter:
    """Returns canned results, or raises whatever was registered."""

    def __init__(self) -> None:
        self.responses: dict[str, SearchResponse] = {}
        self.errors: dict[str, Exception] = {}
        self.queries: list[SearchQuery] = []
        self.default = SearchResponse(
            query="",
            backend="google",
            results=[
                SearchResult(
                    title="Result One",
                    url="https://a.example.com/1",
                    snippet="First snippet.",
                    rank=1,
                ),
                SearchResult(
                    title="Result Two",
                    url="https://b.example.com/2",
                    snippet="Second snippet.",
                    rank=2,
                ),
            ],
        )

    async def search(self, query: SearchQuery) -> SearchResponse:
        self.queries.append(query)
        if query.query in self.errors:
            raise self.errors[query.query]
        if query.query in self.responses:
            return self.responses[query.query]
        return SearchResponse(
            query=query.query, backend=self.default.backend, results=self.default.results
        )


@pytest.fixture
def fake_summarizer():
    return FakeSummarizer()


@pytest.fixture
def fake_pages():
    return FakePageFetcher()


@pytest.fixture
def fake_search():
    return FakeSearchRouter()


@pytest.fixture
def registry():
    """An empty registry.

    These tests fake the summariser, so nothing reaches a real provider. It is
    present because the routes depend on it to resolve a background job's
    credential up front.
    """
    return ModelRegistry([], default_model="anthropic:claude-opus-5", cache_ttl_seconds=300.0)


@pytest.fixture
def job_runner():
    """A real runner - jobs are the thing under test, so they are not faked."""
    return JobRunner(InMemoryJobStore(retention_seconds=1000.0, max_jobs=100), max_concurrent=4)


@pytest.fixture
def app(fake_summarizer, fake_pages, fake_search, job_runner, registry):
    """An app with every external dependency replaced by a fake."""
    application = create_app(make_settings())
    application.dependency_overrides[deps.get_summarizer] = lambda: fake_summarizer
    application.dependency_overrides[deps.get_page_fetcher] = lambda: fake_pages
    application.dependency_overrides[deps.get_search_router] = lambda: fake_search
    application.dependency_overrides[deps.get_max_concurrency] = lambda: 4
    application.dependency_overrides[deps.get_job_runner] = lambda: job_runner
    application.dependency_overrides[deps.get_registry] = lambda: registry
    application.dependency_overrides[deps.get_caller] = lambda: None
    application.dependency_overrides[deps.get_readiness_components] = lambda: [
        ReadinessComponent(name="browser", ready=True, detail="up"),
        ReadinessComponent(name="llm", ready=True, detail="1 model"),
    ]
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
