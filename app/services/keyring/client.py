"""Asking keyring for a usable credential.

Keyring returns *what to attach* - headers and query parameters - never the
stored secret. This client therefore never sees an API key as a value it could
log or persist; it sees a header it is meant to forward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app.core.errors import AuthError, ProviderUnavailableError, TimeoutProblem
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Where the end user's token travels, matching keyring's own header name.
USER_TOKEN_HEADER = "X-Keyring-User-Token"  # noqa: S105 - a header name


@dataclass(frozen=True, slots=True)
class ResolvedAuth:
    """What to attach to an outgoing request for one user and service."""

    headers: dict[str, str] = field(default_factory=dict, repr=False)
    query_params: dict[str, str] = field(default_factory=dict, repr=False)
    expires_at: datetime | None = None

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to attach, as for a keyless local runtime."""
        return not self.headers and not self.query_params

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """Whether this credential has stopped working.

        API keys report no expiry and are never expired; only refreshed OAuth
        tokens carry one.
        """
        if self.expires_at is None:
            return False
        moment = now if now is not None else datetime.now(UTC)
        return moment >= self.expires_at


#: An empty credential, for providers that need none.
NO_AUTH = ResolvedAuth()


class NotConnectedError(Exception):
    """This account has not connected the requested service.

    Not a :class:`~app.core.errors.DomainError`: for the catalogue it is the
    normal case - most people connect a handful of the fifty providers - so it
    is handled where it is raised rather than surfacing to a caller.
    """


class KeyringClient:
    """Resolves credentials from keyring on behalf of an end user."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        service_token: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Create the client.

        Args:
            client: Shared HTTP client.
            base_url: Keyring's root URL.
            service_token: This service's own token, proving to keyring which
                service is calling. Distinct from the user token, which proves
                who it is calling for.
            timeout_seconds: Per-request timeout.
        """
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout = timeout_seconds

    @property
    def is_configured(self) -> bool:
        """Whether this deployment can reach keyring at all."""
        return bool(self._base_url and self._service_token)

    async def resolve(self, *, profile: str, service: str, user_token: str) -> ResolvedAuth:
        """Get what to attach for one user, profile and service.

        Args:
            profile: Which credential set to draw from.
            service: The service name, which is also the provider key here.
            user_token: The end user's short-lived token.

        Returns:
            Headers and query parameters to attach.

        Raises:
            NotConnectedError: The account has no credential for this service.
            AuthError: Keyring rejected the service or user token.
            ProviderUnavailableError: Keyring is unreachable, or the credential
                could not be made usable.
            TimeoutProblem: Keyring did not answer in time.
        """
        url = f"{self._base_url}/v1/internal/credentials/{profile}/{service}"
        try:
            response = await self._client.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._service_token}",
                    USER_TOKEN_HEADER: user_token,
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutProblem(
                "Keyring timed out", detail=f"No answer within {self._timeout}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                "Keyring unreachable", detail=f"{self._base_url}: {exc}"
            ) from exc

        if response.status_code == 404:
            raise NotConnectedError(service)

        if response.status_code == 401:
            raise AuthError(
                "Keyring rejected the credentials",
                detail=_detail(response, "The user token or this service's token was refused."),
            )

        if response.status_code >= 400:
            # 503 in particular: the vault is sealed, the grant was revoked, or a
            # refresh failed. Keyring's detail names the fix, so pass it through.
            raise ProviderUnavailableError(
                "Keyring could not provide the credential",
                detail=_detail(response, f"Keyring responded {response.status_code}."),
            )

        return _to_auth(response)


def _to_auth(response: httpx.Response) -> ResolvedAuth:
    """Parse a resolved-credential payload."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderUnavailableError("Keyring returned invalid JSON", detail=str(exc)) from exc

    if not isinstance(payload, dict):
        raise ProviderUnavailableError(
            "Keyring returned an unexpected payload", detail="Expected an object."
        )

    return ResolvedAuth(
        headers=_string_map(payload.get("headers")),
        query_params=_string_map(payload.get("query_params")),
        expires_at=_parse_expiry(payload.get("expires_at")),
    )


def _string_map(value: object) -> dict[str, str]:
    """Coerce a payload field into a string-to-string mapping."""
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _parse_expiry(value: object) -> datetime | None:
    """Parse an ISO-8601 expiry, tolerating a trailing ``Z``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.info("keyring.unparseable_expiry", value=value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _detail(response: httpx.Response, fallback: str) -> str:
    """Pull keyring's problem detail out of an error response."""
    try:
        payload = response.json()
    except ValueError:
        return fallback
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("title")
        if isinstance(detail, str) and detail:
            return detail
    return fallback
