"""Plain HTTP fetching with SSRF re-validation on every redirect hop.

Redirects are followed manually rather than by httpx, because a one-shot check
of the caller's URL is worthless if a public URL is allowed to redirect to
``http://127.0.0.1``. Each hop goes back through the same validator.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.core.errors import ForbiddenUrlError, TimeoutProblem, UpstreamError
from app.core.logging import get_logger
from app.services.urlsafety import Resolver, default_resolver, validate_url

logger = get_logger(__name__)

#: Content types we are willing to treat as documents.
_TEXTUAL_PREFIXES = ("text/", "application/xhtml", "application/xml", "application/json")


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A successfully fetched document."""

    url: str
    final_url: str
    status_code: int
    content_type: str
    body: str

    @property
    def redirected(self) -> bool:
        """Whether the fetch ended somewhere other than where it started."""
        return self.url != self.final_url


class HttpFetcher:
    """Fetches documents over HTTP with size, redirect and SSRF limits."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        timeout_seconds: float,
        max_redirects: int,
        max_response_bytes: int,
        allow_private: bool = False,
        resolver: Resolver = default_resolver,
    ) -> None:
        """Create the fetcher with its safety limits."""
        self._client = client
        self._user_agent = user_agent
        self._timeout = timeout_seconds
        self._max_redirects = max_redirects
        self._max_bytes = max_response_bytes
        self._allow_private = allow_private
        self._resolver = resolver

    def _validate(self, url: str) -> str:
        """Run a URL through the SSRF guard with this fetcher's policy."""
        return validate_url(url, resolver=self._resolver, allow_private=self._allow_private)

    async def fetch(self, url: str) -> FetchResult:
        """Fetch ``url``, following redirects safely.

        Args:
            url: The URL to fetch. Validated before the first request.

        Returns:
            The fetched document.

        Raises:
            ForbiddenUrlError: A hop pointed somewhere we refuse to go, or the
                redirect budget was exhausted.
            TimeoutProblem: The origin did not answer in time.
            UpstreamError: The origin errored, or returned something unreadable.
        """
        original = self._validate(url)
        current = original

        for _ in range(self._max_redirects + 1):
            response = await self._request(current)

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise UpstreamError(
                        "Malformed redirect",
                        detail=f"{current} returned {response.status_code} with no Location.",
                    )
                current = self._validate(urljoin(current, location))
                continue

            return self._to_result(original, current, response)

        raise ForbiddenUrlError(
            "Too many redirects",
            detail=f"{original} exceeded the limit of {self._max_redirects} redirects.",
        )

    async def _request(self, url: str) -> httpx.Response:
        """Issue a single request, translating transport failures."""
        try:
            return await self._client.get(
                url,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                },
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutProblem(
                "Fetch timed out", detail=f"{url} did not respond within {self._timeout}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("Fetch failed", detail=f"{url}: {exc}") from exc

    def _to_result(self, original: str, final: str, response: httpx.Response) -> FetchResult:
        """Validate a terminal response and turn it into a :class:`FetchResult`."""
        if response.status_code >= 400:
            raise UpstreamError(
                "Origin returned an error",
                detail=f"{final} responded {response.status_code}.",
            )

        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type and not content_type.startswith(_TEXTUAL_PREFIXES):
            raise UpstreamError(
                "Unsupported content type",
                detail=f"{final} returned {content_type}, which is not a readable document.",
            )

        body = response.content[: self._max_bytes].decode(
            response.encoding or "utf-8", errors="replace"
        )
        if len(response.content) > self._max_bytes:
            logger.info("fetch.truncated_body", url=final, limit=self._max_bytes)

        return FetchResult(
            url=original,
            final_url=final,
            status_code=response.status_code,
            content_type=content_type,
            body=body,
        )
