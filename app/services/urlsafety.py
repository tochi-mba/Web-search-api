"""SSRF protection for caller-supplied URLs.

This service fetches URLs chosen by whoever calls the API, which is the textbook
setup for server-side request forgery: without checks, a caller can point it at
``169.254.169.254`` and read cloud credentials, or sweep an internal network.

The guard therefore rejects anything that is not a public http(s) endpoint, and
is applied again on every redirect hop, because a public URL redirecting to
``127.0.0.1`` is the obvious way around a one-shot check.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from app.core.errors import ForbiddenUrlError, ValidationProblem

#: Schemes we are willing to fetch.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Hostnames that expose cloud instance credentials. Blocked by name as well as
#: by address, since the address check alone can be defeated by DNS games.
CLOUD_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "metadata",
    }
)

#: Hostnames that always mean "this machine".
_LOCAL_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

#: Resolves a hostname to a list of IP address strings.
Resolver = Callable[[str], list[str]]


def default_resolver(host: str) -> list[str]:
    """Resolve ``host`` to every address the system knows about."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def normalise_url(url: str) -> str:
    """Canonicalise a URL: lowercase scheme and host, root path, no fragment."""
    parts = urlsplit(url.strip())
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path or "/",
            parts.query,
            "",
        )
    )


def assert_public_ip(address: str, *, host: str) -> None:
    """Raise unless ``address`` is a routable public IP.

    Args:
        address: The IP address to check, as a string.
        host: The hostname it came from, used only for the error message.

    Raises:
        ForbiddenUrlError: If the address is private, loopback, link-local,
            multicast, reserved or otherwise not globally routable.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:  # pragma: no cover - defensive, resolvers return valid IPs
        raise ForbiddenUrlError(
            "Unresolvable address", detail=f"{host} produced an invalid address."
        ) from exc

    if not ip.is_global or ip.is_multicast:
        raise ForbiddenUrlError(
            "Blocked address",
            detail=(
                f"{host} resolves to {address}, which is a private, loopback, "
                "link-local or otherwise non-public address."
            ),
        )


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the parsed IP if ``host`` is already a literal address."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def validate_url(
    url: str,
    *,
    resolver: Resolver = default_resolver,
    allow_private: bool = False,
) -> str:
    """Validate a caller-supplied URL and return its normalised form.

    Args:
        url: The URL to check.
        resolver: Hostname resolver, injected so tests need no real DNS.
        allow_private: Permit private and loopback addresses. Intended for
            trusted internal deployments; metadata hosts stay blocked regardless.

    Returns:
        The normalised URL, safe to fetch.

    Raises:
        ValidationProblem: The URL is malformed or uses a disallowed scheme.
        ForbiddenUrlError: The URL points somewhere we refuse to go.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise ValidationProblem("Malformed URL", detail=str(exc)) from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValidationProblem(
            "Unsupported URL scheme",
            detail=f"Only {', '.join(sorted(ALLOWED_SCHEMES))} URLs may be fetched.",
        )

    host = parts.hostname
    if not host:
        raise ValidationProblem("Malformed URL", detail="The URL has no host component.")

    host = host.lower()

    if host in CLOUD_METADATA_HOSTS:
        raise ForbiddenUrlError(
            "Blocked host",
            detail=f"{host} is a cloud metadata endpoint and is never fetched.",
        )

    if host in _LOCAL_HOSTS and not allow_private:
        raise ForbiddenUrlError("Blocked host", detail=f"{host} refers to this machine.")

    literal = _literal_ip(host)
    if literal is not None:
        if not allow_private:
            assert_public_ip(str(literal), host=host)
        return normalise_url(url)

    try:
        addresses = resolver(host)
    except OSError as exc:
        raise ForbiddenUrlError("Unresolvable host", detail=f"Could not resolve {host}.") from exc

    if not addresses:
        raise ForbiddenUrlError("Unresolvable host", detail=f"{host} resolved to no addresses.")

    if not allow_private:
        for address in addresses:
            assert_public_ip(address, host=host)

    return normalise_url(url)
