"""Resolving credentials from keyring."""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.core.errors import AuthError, ProviderUnavailableError, TimeoutProblem
from app.services.keyring.client import (
    NO_AUTH,
    KeyringClient,
    NotConnectedError,
    ResolvedAuth,
)
from tests.fake_keyring import BASE_URL, FakeKeyring

CRED_URL = f"{BASE_URL}/v1/internal/credentials/personal/anthropic"


@pytest.fixture
def keyring():
    return FakeKeyring()


@pytest.fixture
async def client(keyring):
    async with httpx.AsyncClient() as http:
        yield KeyringClient(http, base_url=BASE_URL, service_token="svc-token", timeout_seconds=5.0)


async def resolve(client, keyring, service="anthropic", profile="personal"):
    return await client.resolve(profile=profile, service=service, user_token=keyring.token())


# --- configuration --------------------------------------------------------- #


async def test_is_configured_requires_both_url_and_token():
    async with httpx.AsyncClient() as http:
        assert KeyringClient(http, base_url=BASE_URL, service_token="t").is_configured
        assert not KeyringClient(http, base_url="", service_token="t").is_configured
        assert not KeyringClient(http, base_url=BASE_URL, service_token="").is_configured


# --- resolving ------------------------------------------------------------- #


@respx.mock
async def test_headers_come_back_ready_to_attach(client, keyring):
    keyring.install(respx.mock)
    keyring.connect("anthropic", headers={"x-api-key": "sk-ant-123"})

    auth = await resolve(client, keyring)
    assert auth.headers == {"x-api-key": "sk-ant-123"}
    assert auth.query_params == {}
    assert auth.is_empty is False


@respx.mock
async def test_query_parameters_come_back(client, keyring):
    keyring.install(respx.mock)
    keyring.connect("gemini", headers={}, query_params={"key": "AIza..."})

    auth = await client.resolve(profile="personal", service="gemini", user_token=keyring.token())
    assert auth.query_params == {"key": "AIza..."}


@respx.mock
async def test_both_credentials_are_sent(client, keyring):
    keyring.install(respx.mock)
    keyring.connect("anthropic")
    await resolve(client, keyring)

    request = respx.calls.last.request
    assert request.headers["authorization"] == "Bearer svc-token"
    assert request.headers["x-keyring-user-token"]


@respx.mock
async def test_the_profile_is_part_of_the_path(client, keyring):
    keyring.install(respx.mock)
    keyring.connect("anthropic", profile="work")
    await resolve(client, keyring, profile="work")
    assert keyring.resolve_calls == [("work", "anthropic")]


@respx.mock
async def test_an_expiry_is_parsed(client, keyring):
    keyring.install(respx.mock)
    keyring.connect("spotify", expires_at="2030-01-01T00:00:00Z")

    auth = await client.resolve(profile="personal", service="spotify", user_token=keyring.token())
    assert auth.expires_at == datetime(2030, 1, 1, tzinfo=UTC)
    assert auth.is_expired() is False


@respx.mock
async def test_an_unparseable_expiry_is_ignored_rather_than_fatal(client, keyring):
    keyring.install(respx.mock)
    keyring.connect("spotify", expires_at="whenever")
    auth = await client.resolve(profile="personal", service="spotify", user_token=keyring.token())
    assert auth.expires_at is None


# --- status mapping -------------------------------------------------------- #


@respx.mock
async def test_a_service_the_account_has_not_connected(client, keyring):
    """The normal case for most of the fifty providers, so not an error."""
    keyring.install(respx.mock)
    with pytest.raises(NotConnectedError):
        await resolve(client, keyring)


@respx.mock
async def test_a_rejected_token_is_an_auth_error(client, keyring):
    respx.get(CRED_URL).mock(
        return_value=httpx.Response(401, json={"detail": "the user token was not accepted"})
    )
    with pytest.raises(AuthError, match="not accepted"):
        await resolve(client, keyring)


@respx.mock
async def test_a_sealed_vault_is_reported_with_keyrings_own_detail(client, keyring):
    """Keyring's detail names the fix, so it must survive the mapping."""
    respx.get(CRED_URL).mock(
        return_value=httpx.Response(503, json={"detail": "the vault is sealed; unseal it"})
    )
    with pytest.raises(ProviderUnavailableError, match="vault is sealed"):
        await resolve(client, keyring)


@respx.mock
async def test_an_unexpected_status_is_reported(client, keyring):
    respx.get(CRED_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderUnavailableError, match="500"):
        await resolve(client, keyring)


@respx.mock
async def test_a_non_json_error_falls_back_to_a_useful_message(client, keyring):
    respx.get(CRED_URL).mock(return_value=httpx.Response(503, content=b"<html>oops"))
    with pytest.raises(ProviderUnavailableError, match="503"):
        await resolve(client, keyring)


@respx.mock
async def test_an_error_body_without_a_detail_falls_back(client, keyring):
    respx.get(CRED_URL).mock(return_value=httpx.Response(503, json={"other": "shape"}))
    with pytest.raises(ProviderUnavailableError, match="503"):
        await resolve(client, keyring)


@respx.mock
async def test_a_title_is_used_when_there_is_no_detail(client, keyring):
    respx.get(CRED_URL).mock(return_value=httpx.Response(401, json={"title": "nope"}))
    with pytest.raises(AuthError, match="nope"):
        await resolve(client, keyring)


@respx.mock
async def test_a_timeout_is_a_timeout(client, keyring):
    respx.get(CRED_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TimeoutProblem):
        await resolve(client, keyring)


@respx.mock
async def test_an_unreachable_keyring_is_reported(client, keyring):
    respx.get(CRED_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await resolve(client, keyring)


@respx.mock
async def test_invalid_json_on_success_is_reported(client, keyring):
    respx.get(CRED_URL).mock(return_value=httpx.Response(200, content=b"not json"))
    with pytest.raises(ProviderUnavailableError, match="invalid JSON"):
        await resolve(client, keyring)


@respx.mock
async def test_a_non_object_payload_is_reported(client, keyring):
    respx.get(CRED_URL).mock(return_value=httpx.Response(200, json=["unexpected"]))
    with pytest.raises(ProviderUnavailableError, match="unexpected payload"):
        await resolve(client, keyring)


@respx.mock
async def test_missing_fields_default_to_empty(client, keyring):
    respx.get(CRED_URL).mock(return_value=httpx.Response(200, json={"service": "anthropic"}))
    auth = await resolve(client, keyring)
    assert auth.is_empty is True


@respx.mock
async def test_non_mapping_header_fields_are_ignored(client, keyring):
    respx.get(CRED_URL).mock(
        return_value=httpx.Response(200, json={"headers": "not a map", "query_params": 7})
    )
    auth = await resolve(client, keyring)
    assert auth.headers == {}
    assert auth.query_params == {}


# --- ResolvedAuth ---------------------------------------------------------- #


def test_no_auth_is_empty():
    assert NO_AUTH.is_empty is True
    assert NO_AUTH.is_expired() is False


def test_resolved_credentials_never_render_their_values():
    auth = ResolvedAuth(
        headers={"Authorization": "secret-header"}, query_params={"key": "secret-query"}
    )
    assert "secret-header" not in repr(auth)
    assert "secret-query" not in str(auth)


def test_caller_never_renders_its_bearer_token():
    from app.services.keyring.caller import Caller

    caller = Caller(account_id="alice", profile="personal", user_token="secret-bearer")
    assert "secret-bearer" not in repr(caller)


def test_expiry_comparison():
    past = ResolvedAuth(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
    future = ResolvedAuth(expires_at=datetime(2100, 1, 1, tzinfo=UTC))
    assert past.is_expired() is True
    assert future.is_expired() is False
    assert future.is_expired(now=datetime(2200, 1, 1, tzinfo=UTC)) is True


def test_a_naive_expiry_is_treated_as_utc():
    from app.services.keyring.client import _parse_expiry

    parsed = _parse_expiry("2030-01-01T00:00:00")
    assert parsed is not None
    assert parsed.tzinfo == UTC


@respx.mock
async def test_a_json_error_that_is_not_an_object_falls_back(client, keyring):
    respx.get(CRED_URL).mock(return_value=httpx.Response(503, json=["a", "list"]))
    with pytest.raises(ProviderUnavailableError, match="503"):
        await resolve(client, keyring)
