import socket

import pytest

from app.core.errors import ForbiddenUrlError, ValidationProblem
from app.services.urlsafety import (
    CLOUD_METADATA_HOSTS,
    assert_public_ip,
    normalise_url,
    validate_url,
)


class FakeResolver:
    """Deterministic stand-in for DNS resolution."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, host):
        self.calls.append(host)
        if host not in self.mapping:
            raise OSError(f"cannot resolve {host}")
        return self.mapping[host]


@pytest.fixture
def resolver():
    return FakeResolver({"example.com": ["93.184.216.34"]})


# --- scheme and shape ------------------------------------------------------ #


@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "javascript:alert(1)"])
def test_non_http_schemes_are_rejected(url, resolver):
    with pytest.raises(ValidationProblem, match="scheme"):
        validate_url(url, resolver=resolver)


def test_missing_host_is_rejected(resolver):
    with pytest.raises(ValidationProblem):
        validate_url("https://", resolver=resolver)


def test_garbage_input_is_rejected(resolver):
    with pytest.raises(ValidationProblem):
        validate_url("not a url at all", resolver=resolver)


def test_valid_public_url_passes(resolver):
    assert validate_url("https://example.com/path?q=1", resolver=resolver) == (
        "https://example.com/path?q=1"
    )


def test_http_is_allowed(resolver):
    assert validate_url("http://example.com/", resolver=resolver).startswith("http://")


# --- normalisation --------------------------------------------------------- #


def test_normalise_lowercases_the_host_and_keeps_the_path():
    assert normalise_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


def test_normalise_strips_fragments():
    assert normalise_url("https://example.com/a#section") == "https://example.com/a"


def test_normalise_adds_a_root_path():
    assert normalise_url("https://example.com") == "https://example.com/"


# --- private address blocking ---------------------------------------------- #


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "0.0.0.0",  # noqa: S104 - the point is that we block it
        "::1",
        "fe80::1",
        "fc00::1",
        "224.0.0.1",
    ],
)
def test_private_and_reserved_addresses_are_blocked(ip):
    with pytest.raises(ForbiddenUrlError):
        assert_public_ip(ip, host="internal.test")


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_addresses_are_allowed(ip):
    assert_public_ip(ip, host="example.com")


def test_hostname_resolving_to_a_private_address_is_blocked():
    resolver = FakeResolver({"sneaky.test": ["10.1.2.3"]})
    with pytest.raises(ForbiddenUrlError, match="private"):
        validate_url("https://sneaky.test/", resolver=resolver)


def test_any_private_address_in_the_answer_blocks_the_host():
    resolver = FakeResolver({"mixed.test": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(ForbiddenUrlError):
        validate_url("https://mixed.test/", resolver=resolver)


def test_literal_private_ip_is_blocked_without_resolution():
    resolver = FakeResolver({})
    with pytest.raises(ForbiddenUrlError):
        validate_url("https://127.0.0.1:8080/admin", resolver=resolver)
    assert resolver.calls == []


def test_unresolvable_host_is_rejected():
    with pytest.raises(ForbiddenUrlError, match="resolve"):
        validate_url("https://nope.invalid/", resolver=FakeResolver({}))


@pytest.mark.parametrize("host", sorted(CLOUD_METADATA_HOSTS))
def test_cloud_metadata_hostnames_are_blocked(host):
    resolver = FakeResolver({host: ["93.184.216.34"]})
    with pytest.raises(ForbiddenUrlError, match="metadata"):
        validate_url(f"https://{host}/latest/meta-data/", resolver=resolver)


def test_localhost_is_blocked():
    with pytest.raises(ForbiddenUrlError):
        validate_url("https://localhost/", resolver=FakeResolver({"localhost": ["127.0.0.1"]}))


# --- escape hatch ---------------------------------------------------------- #


def test_private_networks_can_be_allowed_explicitly():
    resolver = FakeResolver({"internal.test": ["10.0.0.1"]})
    assert (
        validate_url("https://internal.test/", resolver=resolver, allow_private=True)
        == "https://internal.test/"
    )


def test_metadata_hosts_stay_blocked_even_when_private_is_allowed():
    resolver = FakeResolver({"metadata.google.internal": ["10.0.0.1"]})
    with pytest.raises(ForbiddenUrlError):
        validate_url("https://metadata.google.internal/", resolver=resolver, allow_private=True)


# --- the real resolver and remaining edge cases ---------------------------- #


def test_default_resolver_resolves_a_real_name():
    from app.services.urlsafety import default_resolver

    addresses = default_resolver("localhost")
    assert addresses
    assert all(isinstance(a, str) for a in addresses)


def test_default_resolver_raises_for_a_bogus_name():
    from app.services.urlsafety import default_resolver

    with pytest.raises(socket.gaierror):
        default_resolver("no-such-host.invalid")


def test_unparseable_url_is_rejected():
    with pytest.raises(ValidationProblem, match="Malformed URL"):
        validate_url("https://[::1", resolver=FakeResolver({}))


def test_host_resolving_to_nothing_is_rejected():
    resolver = FakeResolver({"empty.test": []})
    with pytest.raises(ForbiddenUrlError, match="no addresses"):
        validate_url("https://empty.test/", resolver=resolver)


def test_public_literal_ip_is_allowed():
    assert validate_url("https://93.184.216.34/x", resolver=FakeResolver({})) == (
        "https://93.184.216.34/x"
    )


def test_private_literal_ip_is_allowed_when_private_is_permitted():
    result = validate_url("https://10.0.0.1/x", resolver=FakeResolver({}), allow_private=True)
    assert result == "https://10.0.0.1/x"
