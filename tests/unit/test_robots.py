import httpx
import pytest
import respx

from app.services.robots import RobotsPolicy

ROBOTS = """
User-agent: *
Disallow: /private/
Allow: /
"""


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def policy(client):
    return RobotsPolicy(client, user_agent="test-agent")


@respx.mock
async def test_allowed_path_is_permitted(policy):
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    assert await policy.can_fetch("https://example.com/public/page") is True


@respx.mock
async def test_disallowed_path_is_refused(policy):
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    assert await policy.can_fetch("https://example.com/private/secret") is False


@respx.mock
async def test_missing_robots_allows_everything(policy):
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    assert await policy.can_fetch("https://example.com/anything") is True


@respx.mock
async def test_network_failure_allows_everything(policy):
    respx.get("https://example.com/robots.txt").mock(side_effect=httpx.ConnectError("boom"))
    assert await policy.can_fetch("https://example.com/anything") is True


@respx.mock
async def test_robots_is_fetched_once_per_origin(policy):
    route = respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS)
    )
    await policy.can_fetch("https://example.com/a")
    await policy.can_fetch("https://example.com/b")
    assert route.call_count == 1


@respx.mock
async def test_distinct_origins_are_fetched_separately(policy):
    a = respx.get("https://a.com/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    b = respx.get("https://b.com/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    await policy.can_fetch("https://a.com/x")
    await policy.can_fetch("https://b.com/x")
    assert a.call_count == 1
    assert b.call_count == 1


@respx.mock
async def test_concurrent_callers_share_a_single_fetch(policy):
    """The second caller must wait on the lock and then reuse the cached parser.

    The response is deliberately slow so the callers genuinely interleave; with
    an instant mock the race this guards against never happens.
    """
    import asyncio

    async def slow_response(request):
        await asyncio.sleep(0.02)
        return httpx.Response(200, text=ROBOTS)

    route = respx.get("https://example.com/robots.txt").mock(side_effect=slow_response)
    results = await asyncio.gather(
        *(policy.can_fetch(f"https://example.com/p{i}") for i in range(5))
    )
    assert route.call_count == 1
    assert all(results)


@respx.mock
async def test_user_agent_specific_rules_are_honoured(client):
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: test-agent\nDisallow: /nope/\n")
    )
    policy = RobotsPolicy(client, user_agent="test-agent")
    assert await policy.can_fetch("https://example.com/nope/x") is False
    assert await policy.can_fetch("https://example.com/yes/x") is True


@respx.mock
async def test_cached_parser_is_reused_without_relocking(policy):
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    assert await policy.can_fetch("https://example.com/a") is True
    # Second call takes the fast path: the parser is already memoised.
    assert await policy.can_fetch("https://example.com/private/x") is False


@respx.mock
async def test_a_lock_is_reused_for_the_same_origin(policy):
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    first = policy._lock_for("https://example.com")
    second = policy._lock_for("https://example.com")
    assert first is second
