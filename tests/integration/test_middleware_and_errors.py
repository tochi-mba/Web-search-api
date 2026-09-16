import httpx
import pytest

from app.core.errors import RateLimitedError, UpstreamError
from app.main import create_app
from tests.conftest import make_settings


def build_client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def secured_app():
    return create_app(make_settings(api_keys=("secret-key",)))


async def test_health_stays_public_when_auth_is_enabled(secured_app):
    async with build_client(secured_app) as client:
        assert (await client.get("/health")).status_code == 200


async def test_protected_route_rejects_missing_key(secured_app):
    @secured_app.get("/v1/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    async with build_client(secured_app) as client:
        response = await client.get("/v1/thing")
    assert response.status_code == 401
    assert response.json()["code"] == "auth_error"
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize(
    "headers",
    [
        {"X-API-Key": "secret-key"},
    ],
)
async def test_protected_route_accepts_valid_credentials(secured_app, headers):
    @secured_app.get("/v1/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    async with build_client(secured_app) as client:
        response = await client.get("/v1/thing", headers=headers)
    assert response.status_code == 200


async def test_wrong_key_is_rejected(secured_app):
    @secured_app.get("/v1/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    async with build_client(secured_app) as client:
        assert (await client.get("/v1/thing", headers={"X-API-Key": "nope"})).status_code == 401


async def test_malformed_authorization_header_is_rejected(secured_app):
    @secured_app.get("/v1/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    async with build_client(secured_app) as client:
        response = await client.get("/v1/thing", headers={"Authorization": "Basic zzz"})
    assert response.status_code == 401


async def test_bearer_identity_does_not_replace_the_api_key_gate(secured_app):
    async with build_client(secured_app) as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer secret-key"})
    assert response.status_code == 401


async def test_domain_errors_render_as_problem_json(settings):
    app = create_app(settings)

    @app.get("/v1/boom")
    async def boom() -> None:
        raise UpstreamError("upstream exploded", detail="503 from origin")

    async with build_client(app) as client:
        response = await client.get("/v1/boom")

    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "upstream_error"
    assert body["detail"] == "503 from origin"
    assert body["instance"] == "/v1/boom"


async def test_domain_error_keeps_an_explicit_instance(settings):
    app = create_app(settings)

    @app.get("/v1/boom")
    async def boom() -> None:
        raise RateLimitedError("slow down", instance="/elsewhere")

    async with build_client(app) as client:
        assert (await client.get("/v1/boom")).json()["instance"] == "/elsewhere"


async def test_unhandled_exceptions_become_500_problems(settings):
    app = create_app(settings)

    @app.get("/v1/kaboom")
    async def kaboom() -> None:
        raise RuntimeError("leaky detail that must not escape")

    async with build_client(app) as client:
        response = await client.get("/v1/kaboom")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert "leaky detail" not in response.text


async def test_request_validation_errors_render_as_problem_json(settings):
    app = create_app(settings)

    @app.get("/v1/needs-param")
    async def needs_param(count: int) -> dict[str, int]:
        return {"count": count}

    async with build_client(app) as client:
        response = await client.get("/v1/needs-param", params={"count": "not-a-number"})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_problem"
    assert body["errors"]
