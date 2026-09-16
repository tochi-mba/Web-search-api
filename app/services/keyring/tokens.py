"""Verify keyring tokens with the family's shared rules and this service's errors."""

from __future__ import annotations

from keyring_client import (
    BAD_TOKEN,
    KEYS_UNAVAILABLE,
    AuthenticationError,
    Clock,
    ExactAudience,
    JwksClient,
    KeyringUnreachableError,
    SystemClock,
    jwks_url,
)
from keyring_client import TokenVerifier as SharedTokenVerifier

from app.core.errors import AuthError, ProviderUnavailableError
from app.core.logging import get_logger

__all__ = ["BAD_TOKEN", "KEYS_UNAVAILABLE", "TokenVerifier"]


class TokenVerifier:
    """Verify locally with pinned issuer, audience and rate-limited key refreshes."""

    def __init__(
        self,
        *,
        base_url: str,
        issuer: str,
        audience: str,
        cache_seconds: float = 3600.0,
        min_refetch_seconds: float = 60.0,
        stale_grace_seconds: float = 86400.0,
        timeout_seconds: float = 10.0,
        clock: Clock | None = None,
    ) -> None:
        """Construct without network I/O; close with the application lifespan."""
        self._audience = ExactAudience(audience)
        clock = clock if clock is not None else SystemClock()
        logger = get_logger(__name__)
        self._jwks = JwksClient(
            url=jwks_url(base_url),
            clock=clock,
            cache_seconds=cache_seconds,
            min_refetch_seconds=min_refetch_seconds,
            stale_grace_seconds=stale_grace_seconds,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
        self._verifier = SharedTokenVerifier(
            jwks=self._jwks, issuer=issuer, clock=clock, logger=logger
        )

    async def verify(self, token: str) -> str:
        """Return the verified account id or an undifferentiated domain refusal."""
        try:
            identity = await self._verifier.verify(token, audience=self._audience)
        except AuthenticationError:
            raise AuthError(BAD_TOKEN) from None
        except KeyringUnreachableError:
            raise ProviderUnavailableError("Keyring unreachable", detail=KEYS_UNAVAILABLE) from None
        return identity.account_id

    async def aclose(self) -> None:
        """Release pooled connections to the signing-key endpoint."""
        await self._jwks.aclose()
