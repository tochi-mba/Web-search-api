"""Domain exceptions and their RFC 9457 ``application/problem+json`` mapping.

Every failure the service can express is a subclass of :class:`DomainError`, so
route handlers never need to know HTTP status codes and batch endpoints can
render the same failure either as a response body or as a per-item error.
"""

from __future__ import annotations

import re
from typing import Any

PROBLEM_BASE_URI = "https://web-search-api.dev/problems"

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _default_code(class_name: str) -> str:
    """Derive a stable snake_case error code from the exception class name."""
    return _CAMEL_BOUNDARY.sub("_", class_name).lower()


class DomainError(Exception):
    """Base class for every expected failure mode of the service."""

    status: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        title: str,
        *,
        detail: str | None = None,
        instance: str | None = None,
    ) -> None:
        """Create the error.

        Args:
            title: Short, human-readable summary of the problem type.
            detail: Explanation specific to this occurrence. Defaults to ``title``.
            instance: URI reference identifying the specific occurrence.
        """
        self.title = title
        self.detail = detail if detail is not None else title
        # Include the detail in args so tracebacks and logs carry the specifics,
        # not just the generic problem class.
        super().__init__(title if self.detail == title else f"{title}: {self.detail}")
        self.instance = instance

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Give each subclass a snake_case ``code`` unless it declares one."""
        super().__init_subclass__(**kwargs)
        if "code" not in cls.__dict__:
            cls.code = _default_code(cls.__name__)

    def to_problem(self) -> dict[str, Any]:
        """Render as an RFC 9457 problem document."""
        problem: dict[str, Any] = {
            "type": f"{PROBLEM_BASE_URI}/{self.code}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "code": self.code,
        }
        if self.instance is not None:
            problem["instance"] = self.instance
        return problem

    def to_error_payload(self) -> dict[str, str]:
        """Render as the compact error object embedded in batch results."""
        return {"code": self.code, "title": self.title, "detail": self.detail}


class ValidationProblem(DomainError):
    """The request was structurally valid but semantically unacceptable."""

    status = 400


class AuthError(DomainError):
    """The caller did not present a valid API key."""

    status = 401


class ForbiddenUrlError(DomainError):
    """The requested URL is blocked by SSRF policy or robots.txt."""

    status = 403


class NotFoundError(DomainError):
    """The requested resource, model or provider does not exist."""

    status = 404


class RateLimitedError(DomainError):
    """An upstream rate limit was hit."""

    status = 429


class UpstreamError(DomainError):
    """An upstream site or API failed in a way we cannot recover from."""

    status = 502


class SearchBlockedError(UpstreamError):
    """The search engine served a consent wall or CAPTCHA instead of results."""


class ProviderUnavailableError(DomainError):
    """No LLM provider capable of serving the request is currently reachable."""

    status = 503


class PreferencesUnavailableError(DomainError):
    """A person's settings were needed and could not be read honestly.

    Either settings-api refused this service -- a grant it was not given, a
    token it does not recognise -- or it cannot be reached and the setting in
    question is one that must not be guessed at. Neither is the caller's doing,
    so it is not a 4xx.
    """

    status = 503


class TimeoutProblem(DomainError):
    """An upstream call exceeded its deadline."""

    status = 504
