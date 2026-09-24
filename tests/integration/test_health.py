import httpx
import pytest

from app import __version__
from app.api import deps
from app.main import create_app
from app.schemas.health import ReadinessComponent


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_reports_ok(client, settings):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == settings.service_name
    assert body["version"] == __version__
    assert body["uptime_seconds"] >= 0


async def test_healthy_alias_matches_health(client):
    assert (await client.get("/healthy")).json()["status"] == "ok"


async def test_health_echoes_request_id(client):
    response = await client.get("/health", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"


async def test_health_generates_a_request_id_when_absent(client):
    assert response_id(await client.get("/health"))


def response_id(response):
    return response.headers.get("X-Request-ID")


async def test_ready_is_degraded_without_dependencies(client):
    response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["status"] == "degraded"


async def test_canonical_ready_is_degraded_without_dependencies(client):
    assert (await client.get("/ready")).status_code == 503


async def test_ready_is_ok_when_every_component_is_ready(app, client):
    app.dependency_overrides[deps.get_readiness_components] = lambda: [
        ReadinessComponent(name="browser", ready=True, detail="up"),
        ReadinessComponent(name="llm", ready=True, detail="3 models"),
    ]
    body = (await client.get("/health/ready")).json()
    assert body["ready"] is True
    assert body["status"] == "ok"
    app.dependency_overrides.clear()


async def test_unknown_path_returns_problem_json(client):
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "http_error"


async def test_lifespan_builds_and_releases_every_service(settings):
    """The composition root must wire everything the routes depend on."""
    from app.main import create_app as _create_app
    from app.main import lifespan
    from app.services.fetch.browser import browser_is_launchable

    application = _create_app(settings)
    async with lifespan(application):
        assert application.state.model_registry is not None
        assert application.state.summarizer is not None
        assert application.state.page_fetcher is not None
        assert application.state.search_router is not None
        # Whether a browser is really there, not whether headless mode is switched on. This
        # asserted `is True` while the flag was `... or settings.browser_headless`, which
        # defaults to true -- so it passed on a machine with no browser at all, which is the
        # machine the service then told `/ready` it was fine on.
        launchable, detail = await browser_is_launchable()
        assert application.state.browser_available is launchable
        assert application.state.browser_detail == detail
        assert detail, "readiness has to be able to say why"
    # Teardown released the client without raising.
    assert application.state.services.http_client.is_closed
