"""Local verification of keyring's user tokens.

Keyring signs short-lived RS256 tokens and publishes the public half at
``/.well-known/jwks.json``, explicitly so that consuming services verify locally
rather than calling back per request.

Verifying here rather than treating the token as opaque buys two things: a
forged or expired token is rejected without a network round trip, and the
verified ``sub`` claim gives a cache key that survives token rotation.
"""

from __future__ import annotations

from typing import Any

import httpx
import jwt

from app.core.cache import TTLCache
from app.core.errors import AuthError, ProviderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Pinned. Leaving the algorithm open is the classic JWT failure: a caller
#: supplies ``alg: none``, or downgrades RS256 to HS256 and signs with the
#: public key we just published.
ALGORITHM = "RS256"

JWKS_PATH = "/.well-known/jwks.json"

#: One message for every failure. A caller holding a forged token learns nothing
#: useful from being told which check it failed.
BAD_TOKEN = "the user token was not accepted"  # noqa: S105 - a message, not a token


class TokenVerifier:
    """Verifies keyring-issued tokens against its published keys."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        audience: str,
        cache_seconds: float = 3600.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Create the verifier.

        Args:
            client: Shared HTTP client.
            base_url: Keyring's root URL.
            audience: This service's name. A token minted for another service
                must not be accepted here, which is what the audience check is for.
            cache_seconds: How long a fetched JWKS document is trusted.
            timeout_seconds: Per-request timeout when fetching keys.
        """
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._audience = audience
        self._timeout = timeout_seconds
        self._jwks: TTLCache[dict[str, Any]] = TTLCache(cache_seconds)

    async def _fetch_jwks(self) -> dict[str, Any]:
        """Fetch keyring's public keys."""
        try:
            response = await self._client.get(f"{self._base_url}{JWKS_PATH}", timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                "Keyring unreachable",
                detail=f"Could not fetch signing keys from {self._base_url}: {exc}",
            ) from exc

        if response.status_code != 200:
            raise ProviderUnavailableError(
                "Keyring unreachable",
                detail=f"{self._base_url}{JWKS_PATH} responded {response.status_code}.",
            )

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError(
                "Keyring returned invalid JWKS", detail=str(exc)
            ) from exc
        return payload

    async def _signing_key(self, token: str, *, allow_refresh: bool = True) -> Any:
        """Find the key that signed ``token``, refreshing once on an unknown id.

        Keyring rotating its key would otherwise mean every token failing until
        the cache expired.
        """
        jwks = await self._jwks.get(self._fetch_jwks)
        try:
            return _match_key(jwks, token)
        except KeyError:
            if not allow_refresh:
                raise AuthError(BAD_TOKEN, detail="No key matches this token's key id.") from None
            logger.info("keyring.jwks_refresh", reason="unknown key id")
            self._jwks.invalidate()
            return await self._signing_key(token, allow_refresh=False)

    async def verify(self, token: str) -> str:
        """Verify a token and return the account id it asserts.

        Raises:
            AuthError: Expired, forged, unsigned, issued for another service, or
                missing a required claim.
            ProviderUnavailableError: Keyring's keys could not be fetched.
        """
        key = await self._signing_key(token)

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except jwt.InvalidTokenError as exc:
            raise AuthError(BAD_TOKEN, detail=str(exc)) from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthError(BAD_TOKEN, detail="The token carries no account id.")
        return subject


def _match_key(jwks: dict[str, Any], token: str) -> Any:
    """Return the JWK matching the token's ``kid``.

    Raises:
        KeyError: No key in the set has that id.
        AuthError: The token is not a well-formed JWT at all.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise AuthError(BAD_TOKEN, detail=str(exc)) from exc

    kid = header.get("kid")
    for entry in jwks.get("keys", []):
        if kid is None or entry.get("kid") == kid:
            return jwt.PyJWK.from_dict(entry).key
    raise KeyError(kid)
