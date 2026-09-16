"""Local verification of keyring's user tokens.

Tested against real RS256 signatures and a real JWKS document. Verification is a security
control; stubbing it would prove only that the stub works. The rules themselves live in
keyring-client, the verifier every service in the family shares. These tests pin that this
service applies them with its own issuer and audience, and that what reaches a caller is this
service's error vocabulary and nothing a forger could learn from.
"""

import base64
import hashlib
import hmac
import json
import time

import httpx
import jwt as pyjwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization

from app.core.errors import AuthError, ProviderUnavailableError
from app.services.keyring.tokens import BAD_TOKEN, KEYS_UNAVAILABLE, TokenVerifier
from tests.fake_keyring import BASE_URL, ISSUER, FakeKeyring, other_key_pem, unsigned_token

JWKS_URL = f"{BASE_URL}/.well-known/jwks.json"


@pytest.fixture
def keyring():
    return FakeKeyring()


@pytest.fixture
async def verifier(keyring):
    verifier = TokenVerifier(
        base_url=BASE_URL, issuer=ISSUER, audience=keyring.audience, cache_seconds=100.0
    )
    yield verifier
    await verifier.aclose()


def _segment(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def forge_hs256(keyring):
    """HS256 signed with the published public key, assembled by hand as an attacker would."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": keyring.key_id}
    claims = {"iss": ISSUER, "sub": "acct-1", "aud": keyring.audience, "iat": now, "exp": now + 300}
    signing_input = f"{_segment(header)}.{_segment(claims)}"
    public_pem = keyring._private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"


# --- the happy path -------------------------------------------------------- #


@respx.mock
async def test_a_valid_token_yields_its_account_id(verifier, keyring):
    keyring.install(respx.mock)
    assert await verifier.verify(keyring.token("acct-42")) == "acct-42"


@respx.mock
async def test_trailing_slash_on_the_base_url_is_tolerated(keyring):
    keyring.install(respx.mock)
    verifier = TokenVerifier(base_url=f"{BASE_URL}/", issuer=ISSUER, audience=keyring.audience)
    try:
        assert await verifier.verify(keyring.token()) == "acct-1"
    finally:
        await verifier.aclose()


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
async def test_a_token_from_another_keyring_is_refused(verifier, keyring):
    """A second keyring -- a staging one, somebody's laptop -- does not speak for this one."""
    keyring.install(respx.mock)
    with pytest.raises(AuthError):
        await verifier.verify(keyring.token(issuer="https://another-keyring.test"))


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
async def test_an_hs256_token_signed_with_the_public_key_is_refused(verifier, keyring):
    """The public key is published for anybody to fetch, so it must never verify HS256."""
    keyring.install(respx.mock)
    with pytest.raises(AuthError):
        await verifier.verify(forge_hs256(keyring))


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
    with pytest.raises(AuthError):
        await verifier.verify(keyring.token(account_id=""))


@respx.mock
async def test_a_token_without_a_key_id_is_refused_rather_than_tried_against_a_key(
    verifier, keyring
):
    """Choosing the key on the sender's behalf is doing their search for them."""
    keyring.install(respx.mock)
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "acct-7",
        "aud": keyring.audience,
        "iat": now,
        "exp": now + 300,
    }
    token = pyjwt.encode(claims, keyring._pem, algorithm="RS256")
    with pytest.raises(AuthError):
        await verifier.verify(token)


@respx.mock
async def test_every_refusal_is_the_same_problem(verifier, keyring):
    """A caller holding a forged token learns nothing from which check refused it."""
    keyring.install(respx.mock)
    refused = [
        keyring.token(issued_at=time.time() - 10_000, ttl_seconds=60),
        keyring.token(key=other_key_pem()),
        keyring.token(audience="some-other-service"),
        keyring.token(issuer="https://another-keyring.test"),
        "not-a-jwt-at-all",
    ]
    problems = []
    for token in refused:
        with pytest.raises(AuthError) as caught:
            await verifier.verify(token)
        problems.append(caught.value.to_problem())

    assert all(problem == problems[0] for problem in problems)
    assert problems[0]["detail"] == BAD_TOKEN


# --- key handling ---------------------------------------------------------- #


@respx.mock
async def test_keys_are_fetched_once_and_cached(verifier, keyring):
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=keyring.jwks()))
    await verifier.verify(keyring.token())
    await verifier.verify(keyring.token())
    assert route.call_count == 1


@respx.mock
async def test_an_unknown_key_id_triggers_one_refresh(verifier, keyring):
    """Keyring replacing its key must not mean an outage until the cache expires."""
    rotated = FakeKeyring(key_id="rotated-key")
    route = respx.get(JWKS_URL)
    route.side_effect = [
        httpx.Response(200, json=keyring.jwks()),
        httpx.Response(200, json=rotated.jwks()),
    ]

    assert await verifier.verify(keyring.token("acct-8")) == "acct-8"
    assert await verifier.verify(rotated.token("acct-9")) == "acct-9"
    assert route.call_count == 2


@respx.mock
async def test_a_key_id_that_never_appears_is_refused(verifier, keyring):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=keyring.jwks()))
    stranger = FakeKeyring(key_id="never-published")
    with pytest.raises(AuthError):
        await verifier.verify(stranger.token())


# --- keyring being unreachable --------------------------------------------- #


@respx.mock
async def test_unreachable_keyring_is_reported_as_unavailable(verifier, keyring):
    respx.get(JWKS_URL).mock(
        side_effect=httpx.ConnectError("refused by https://user:hunter2@keyring.test")
    )
    with pytest.raises(ProviderUnavailableError, match="unreachable") as caught:
        await verifier.verify(keyring.token())

    # The HTTP client's text carries the URL, and a URL can carry credentials.
    assert caught.value.detail == KEYS_UNAVAILABLE
    assert "hunter2" not in str(caught.value)


@respx.mock
async def test_an_error_status_on_jwks_is_reported(verifier, keyring):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await verifier.verify(keyring.token())


@respx.mock
async def test_invalid_jwks_json_is_reported(verifier, keyring):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, content=b"not json"))
    with pytest.raises(ProviderUnavailableError) as caught:
        await verifier.verify(keyring.token())
    assert caught.value.detail == KEYS_UNAVAILABLE
