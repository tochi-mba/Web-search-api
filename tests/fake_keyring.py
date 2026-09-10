"""A fake keyring that signs real tokens.

Verification is a security control, so it is tested against genuine RS256
signatures and a genuine JWKS document rather than a stubbed verifier. A test
that patches the verifier proves only that the patch works.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

BASE_URL = "https://keyring.test"
ISSUER = "keyring"
AUDIENCE = "web-search-api"


def _b64(value: int) -> str:
    """Base64url-encode an RSA parameter the way a JWKS expects."""
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass
class FakeKeyring:
    """Mints tokens, publishes keys, and answers credential lookups."""

    key_id: str = "test-key"
    issuer: str = ISSUER
    audience: str = AUDIENCE
    #: (account_id, profile, service) -> the resolved payload to return.
    credentials: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    #: Services that should answer with a status instead, e.g. 503.
    failures: dict[str, int] = field(default_factory=dict)
    resolve_calls: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Generate the signing key this instance will use."""
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._pem = self._private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    # -- minting ----------------------------------------------------------- #

    def token(
        self,
        account_id: str = "acct-1",
        *,
        audience: str | None = None,
        issuer: str | None = None,
        ttl_seconds: float = 300.0,
        issued_at: float | None = None,
        key: bytes | None = None,
        omit: str | None = None,
    ) -> str:
        """Mint a token, with knobs for every way one can be wrong."""
        now = issued_at if issued_at is not None else time.time()
        claims: dict[str, Any] = {
            "iss": issuer if issuer is not None else self.issuer,
            "sub": account_id,
            "aud": audience if audience is not None else self.audience,
            "iat": int(now),
            "exp": int(now + ttl_seconds),
        }
        if omit:
            claims.pop(omit, None)
        signing_key = key if key is not None else self._pem
        return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": self.key_id})

    def jwks(self) -> dict[str, Any]:
        """The public half, in JWKS form."""
        numbers = self._private.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.key_id,
                    "n": _b64(numbers.n),
                    "e": _b64(numbers.e),
                }
            ]
        }

    # -- credentials ------------------------------------------------------- #

    def connect(
        self,
        service: str,
        *,
        account_id: str = "acct-1",
        profile: str = "personal",
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        expires_at: str | None = None,
    ) -> None:
        """Register a credential this account has connected."""
        self.credentials[(account_id, profile, service)] = {
            "service": service,
            "headers": headers
            if headers is not None
            else {"Authorization": f"Bearer {service}-key"},
            "query_params": query_params or {},
            "expires_at": expires_at,
        }

    def fail(self, service: str, status: int) -> None:
        """Make one service answer with an error status."""
        self.failures[service] = status

    # -- routing ----------------------------------------------------------- #

    def install(self, respx_mock: Any) -> None:
        """Register routes on a respx mock router."""
        respx_mock.get(f"{BASE_URL}/.well-known/jwks.json").mock(
            side_effect=lambda request: httpx.Response(200, json=self.jwks())
        )
        respx_mock.get(url__regex=rf"{BASE_URL}/v1/internal/credentials/.*").mock(
            side_effect=self._resolve
        )

    def _resolve(self, request: httpx.Request) -> httpx.Response:
        """Answer a credential lookup the way keyring would."""
        _, profile, service = request.url.path.rsplit("/", 2)
        self.resolve_calls.append((profile, service))

        token = request.headers.get("X-Keyring-User-Token")
        if not token:
            return httpx.Response(401, json={"detail": "the user token was not accepted"})
        if not request.headers.get("Authorization"):
            return httpx.Response(401, json={"detail": "service credentials were not accepted"})

        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.InvalidTokenError:  # pragma: no cover - tests always send a real token
            return httpx.Response(401, json={"detail": "the user token was not accepted"})

        if service in self.failures:
            return httpx.Response(
                self.failures[service], json={"detail": f"{service} is unavailable"}
            )

        payload = self.credentials.get((claims["sub"], profile, service))
        if payload is None:
            return httpx.Response(404, json={"detail": f"{service} is not connected"})
        return httpx.Response(200, json=payload)


def other_key_pem() -> bytes:
    """A different private key, for forged-signature tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def unsigned_token(account_id: str = "acct-1") -> str:
    """A token with ``alg: none`` - the downgrade attack a verifier must refuse."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    claims = base64.urlsafe_b64encode(
        json.dumps(
            {
                "iss": ISSUER,
                "sub": account_id,
                "aud": AUDIENCE,
                "iat": int(time.time()),
                "exp": int(time.time() + 300),
            }
        ).encode()
    )
    return f"{header.decode().rstrip('=')}.{claims.decode().rstrip('=')}."
