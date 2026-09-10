"""Composition root: builds every long-lived service from settings.

Kept apart from ``main`` so the wiring can be exercised directly in tests
without starting an HTTP server.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import Settings
from app.core.logging import get_logger
from app.services.fetch.browser import BrowserSession, PlaywrightBrowserSession
from app.services.fetch.http import HttpFetcher
from app.services.fetch.page import PageFetcher
from app.services.jobs.memory import InMemoryJobStore
from app.services.jobs.runner import JobRunner
from app.services.llm.base import LLMProvider
from app.services.llm.providers.anthropic import AnthropicProvider
from app.services.llm.providers.ollama import OllamaProvider
from app.services.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.services.llm.registry import ModelRegistry
from app.services.llm.specs import OPENAI_COMPATIBLE_SPECS
from app.services.llm.summarizer import Summarizer
from app.services.robots import RobotsPolicy
from app.services.search.base import SearchBackend
from app.services.search.google import GoogleSearchBackend
from app.services.search.router import SearchRouter
from app.services.search.searxng import SearxngSearchBackend
from app.services.search.serper import SerperSearchBackend

logger = get_logger(__name__)


@dataclass
class Services:
    """Every long-lived object the application needs."""

    http_client: httpx.AsyncClient
    browser: PlaywrightBrowserSession
    registry: ModelRegistry
    summarizer: Summarizer
    page_fetcher: PageFetcher
    search_router: SearchRouter
    job_runner: JobRunner
    anthropic: AnthropicProvider

    async def aclose(self) -> None:
        """Release every resource, best effort."""
        # Jobs first: cancelling them while their collaborators still exist
        # leaves no task reaching for a closed client on the way out.
        await self.job_runner.aclose()
        await self.browser.close()
        await self.anthropic.aclose()
        await self.http_client.aclose()


def build_llm_providers(
    settings: Settings, client: httpx.AsyncClient
) -> tuple[list[LLMProvider], AnthropicProvider]:
    """Instantiate every LLM provider this deployment knows about.

    Providers are always constructed; whether they are *usable* is decided by
    probing, not by configuration, so a wrong key shows up as a status rather
    than a silently missing vendor.
    """
    disabled = set(settings.disabled_providers)

    anthropic = AnthropicProvider(timeout_seconds=settings.request_timeout_seconds * 3)
    providers: list[LLMProvider] = []

    if anthropic.name not in disabled:
        providers.append(anthropic)

    if "ollama" not in disabled:
        providers.append(
            OllamaProvider(
                client,
                base_url=settings.provider_base_urls.get("ollama"),
                timeout_seconds=settings.request_timeout_seconds * 3,
            )
        )

    for spec in OPENAI_COMPATIBLE_SPECS:
        if spec.key in disabled:
            continue
        providers.append(
            OpenAICompatibleProvider(
                spec,
                client,
                base_url=settings.provider_base_urls.get(spec.key),
                timeout_seconds=settings.request_timeout_seconds * 3,
            )
        )

    return providers, anthropic


def build_search_backends(
    settings: Settings, client: httpx.AsyncClient, browser: BrowserSession
) -> list[SearchBackend]:
    """Instantiate every search backend, in default preference order."""
    return [
        GoogleSearchBackend(browser),
        SearxngSearchBackend(
            client,
            base_url=settings.searxng_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        SerperSearchBackend(
            client,
            api_key=settings.serper_api_key,
            timeout_seconds=settings.request_timeout_seconds,
        ),
    ]


def build_services(settings: Settings) -> Services:
    """Build every long-lived service from settings."""
    client = httpx.AsyncClient(
        follow_redirects=False,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        headers={"User-Agent": settings.user_agent},
    )

    browser = PlaywrightBrowserSession(
        headless=settings.browser_headless,
        navigation_timeout_ms=settings.browser_navigation_timeout_ms,
        user_agent=settings.user_agent,
    )

    providers, anthropic = build_llm_providers(settings, client)
    registry = ModelRegistry(
        providers,
        default_model=settings.default_model,
        cache_ttl_seconds=settings.model_cache_ttl_seconds,
        probe_timeout_seconds=settings.provider_probe_timeout_seconds,
        max_concurrency=settings.max_concurrency,
    )

    summarizer = Summarizer(registry, max_content_chars=settings.max_content_chars)

    http_fetcher = HttpFetcher(
        client,
        user_agent=settings.user_agent,
        timeout_seconds=settings.request_timeout_seconds,
        max_redirects=settings.max_redirects,
        max_response_bytes=settings.max_response_bytes,
        allow_private=settings.allow_private_networks,
    )

    page_fetcher = PageFetcher(
        http_fetcher,
        browser=browser,
        robots=RobotsPolicy(client, user_agent=settings.user_agent),
        respect_robots=settings.respect_robots,
        allow_private=settings.allow_private_networks,
    )

    search_router = SearchRouter(
        build_search_backends(settings, client, browser),
        preferred=settings.search_backend,
    )

    job_runner = JobRunner(
        InMemoryJobStore(
            retention_seconds=settings.job_retention_seconds,
            max_jobs=settings.max_stored_jobs,
        ),
        max_concurrent=settings.max_background_jobs,
    )

    logger.info(
        "services.built",
        providers=len(providers),
        default_model=settings.default_model,
        search_backend=settings.search_backend,
    )

    return Services(
        http_client=client,
        browser=browser,
        registry=registry,
        summarizer=summarizer,
        page_fetcher=page_fetcher,
        search_router=search_router,
        job_runner=job_runner,
        anthropic=anthropic,
    )
