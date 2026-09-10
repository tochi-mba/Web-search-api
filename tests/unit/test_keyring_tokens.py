"""Local verification of keyring's user tokens.

Tested against real RS256 signatures and a real JWKS document. Verification is
a security control; stubbing it would prove only that the stub works.
"""

import time

import httpx
import pytest
import respx

from app.core.errors import AuthError, ProviderUnavailableError
from app.services.keyring.tokens import TokenVerifier
from tests.fake_keyring import BASE_URL, FakeKeyring, other_key_pem, unsigned_token


@pytest.fixture
def keyring():
    return FakeKeyring()


@pytest.fixture
async def verifier(keyring):
    async with httpx.AsyncClient() as client:
        yield TokenVerifier(
            client, base_url=BASE_URL, audience=keyring.audience, cache_seconds=100.0
        )


# --- the happy path -------------------------------------------------------- #


@respx.mock
async def test_a_valid_token_yields_its_account_id(verifier, keyring):
    keyring.install(respx.mock)
    assert await verifier.verify(keyring.token("acct-42")) == "acct-42"


@respx.mock
async def test_trailing_slash_on_the_base_url_is_tolerated(keyring):
    keyring.install(respx.mock)
    async with httpx.AsyncClient() as client:
        verifier = TokenVerifier(client, base_url=f"{BASE_URL}/", audience=keyring.audience)
        assert await verifier.verify(keyring.token()) == "acct-1"


# --- tokens that must be refused ------------------------------------------- #


@respx.mock
async def test_an_expired_token_is_refused(verifier, keyring):
    keyring.install(respx.mock)
    stale = keyring.token(issued_at=time.time() - 10_000, ttl_seconds=60)
    with pytest.raises(AuthError):
        await verifier.verify(stale)


@respx.mock
async def test_a_token_for_another_service_is_refused(verifier, keyring):
    """A token minted for one service must not be replayable at another."""
    keyring.install(respx.mock)
    with pytest.raises(AuthError):
        await verifier.verify(keyring.token(audience="some-other-service"))


@respx.mock
async def test_a_forged_signature_is_refused(verifier, keyring):
    keyring.install(respx.mock)
    forged = keyring.token(key=other_key_pem())
    with pytest.raises(AuthError):
        await verifier.verify(forged)


@respx.mock
async def test_an_unsigned_token_is_refused(verifier, keyring):
    """The alg:none downgrade is the classic JWT failure."""
    keyring.install(respx.mock)
    with pytest.raises(AuthError):
        await verifier.verify(unsigned_token())


@respx.mock
@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "sub", "aud"])
async def test_a_token_missing_a_required_claim_is_refused(verifier, keyring, claim):
    keyring.install(respx.mock)
    with pytest.raises(AuthError):
        await verifier.verify(keyring.token(omit=claim))


@respx.mock
async def test_garbage_is_refused(verifier, keyring):
    keyring.install(respx.mock)
    with pytest.raises(AuthError):
        await verifier.verify("not-a-jwt-at-all")


@respx.mock
async def test_an_empty_subject_is_refused(verifier, keyring):
    keyring.install(respx.mock)
    with pytest.raises(AuthError, match="no account id"):
        await verifier.verify(keyring.token(account_id=""))


@respx.mock
async def test_failures_do_not_say_which_check_failed(verifier, keyring):
    """A caller holding a forged token learns nothing useful from the title."""
    keyring.install(respx.mock)
    expired = keyring.token(issued_at=time.time() - 10_000, ttl_seconds=60)

    with pytest.raises(AuthError) as expired_exc:
        await verifier.verify(expired)
    with pytest.raises(AuthError) as forged_exc:
        await verifier.verify(keyring.token(key=other_key_pem()))

    assert expired_exc.value.title == forged_exc.value.title


# --- key handling ---------------------------------------------------------- #


@respx.mock
async def test_keys_are_fetched_once_and_cached(verifier, keyring):
    route = respx.get(f"{BASE_URL}/.well-known/jwks.json").mock(
        return_value=httpx.Response(200, json=keyring.jwks())
    )
    await verifier.verify(keyring.token())
    await verifier.verify(keyring.token())
    assert route.call_count == 1


@respx.mock
async def test_an_unknown_key_id_triggers_one_refresh(verifier, keyring):
    """Keyring rotating its key must not mean an outage until the cache expires."""
    rotated = FakeKeyring(key_id="rotated-key")
    responses = [
        httpx.Response(200, json=keyring.jwks()),
        httpx.Response(200, json=rotated.jwks()),
    ]
    route = respx.get(f"{BASE_URL}/.well-known/jwks.json")
    route.side_effect = responses

    assert await verifier.verify(rotated.token("acct-9")) == "acct-9"
    assert route.call_count == 2


@respx.mock
async def test_a_key_id_that_never_appears_is_refused(verifier, keyring):
    respx.get(f"{BASE_URL}/.well-known/jwks.json").mock(
        return_value=httpx.Response(200, json=keyring.jwks())
    )
    stranger = FakeKeyring(key_id="never-published")
    with pytest.raises(AuthError):
        await verifier.verify(stranger.token())


@respx.mock
async def test_a_token_without_a_key_id_uses_the_only_key(verifier, keyring):
    respx.get(f"{BASE_URL}/.well-known/jwks.json").mock(
        return_value=httpx.Response(200, json=keyring.jwks())
    )
    import jwt as pyjwt

    token = pyjwt.encode(
        {
            "iss": keyring.issuer,
            "sub": "acct-7",
            "aud": keyring.audience,
            "iat": int(time.time()),
            "exp": int(time.time() + 300),
        },
        keyring._pem,
        algorithm="RS256",
    )
    assert await verifier.verify(token) == "acct-7"


# --- keyring being unreachable --------------------------------------------- #


@respx.mock
async def test_unreachable_keyring_is_reported_as_unavailable(verifier, keyring):
    respx.get(f"{BASE_URL}/.well-known/jwks.json").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await verifier.verify(keyring.token())


@respx.mock
async def test_an_error_status_on_jwks_is_reported(verifier, keyring):
    respx.get(f"{BASE_URL}/.well-known/jwks.json").mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await verifier.verify(keyring.token())


@respx.mock
async def test_invalid_jwks_json_is_reported(verifier, keyring):
    respx.get(f"{BASE_URL}/.well-known/jwks.json").mock(
        return_value=httpx.Response(200, content=b"not json")
    )
    with pytest.raises(ProviderUnavailableError, match="invalid JWKS"):
        await verifier.verify(keyring.token())
