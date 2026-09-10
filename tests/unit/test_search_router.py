import pytest

from app.core.errors import ProviderUnavailableError, SearchBlockedError, UpstreamError
from app.services.search.base import SearchQuery, SearchResponse, SearchResult
from app.services.search.router import SearchRouter

QUERY = SearchQuery(query="widget latency")


class StubBackend:
    def __init__(self, name, *, configured=True, results=None, error=None):
        self.name = name
        self._configured = configured
        self._results = results or []
        self._error = error
        self.calls = 0

    async def is_configured(self):
        return self._configured

    async def search(self, query):
        self.calls += 1
        if self._error:
            raise self._error
        return SearchResponse(query=query.query, backend=self.name, results=self._results)


def result(url="https://a.com/1"):
    return [SearchResult(title="T", url=url, snippet="s", rank=1)]


async def test_uses_the_first_configured_backend():
    primary = StubBackend("google", results=result())
    secondary = StubBackend("searxng", results=result())
    response = await SearchRouter([primary, secondary]).search(QUERY)
    assert response.backend == "google"
    assert secondary.calls == 0


async def test_preferred_backend_is_tried_first():
    google = StubBackend("google", results=result())
    serper = StubBackend("serper", results=result())
    router = SearchRouter([google, serper], preferred="serper")
    assert (await router.search(QUERY)).backend == "serper"
    assert google.calls == 0


async def test_unknown_preferred_name_falls_back_to_declared_order():
    google = StubBackend("google", results=result())
    router = SearchRouter([google], preferred="does-not-exist")
    assert (await router.search(QUERY)).backend == "google"


async def test_unconfigured_backends_are_skipped():
    unconfigured = StubBackend("serper", configured=False)
    configured = StubBackend("google", results=result())
    assert (await SearchRouter([unconfigured, configured]).search(QUERY)).backend == "google"
    assert unconfigured.calls == 0


async def test_failover_when_the_primary_is_blocked():
    blocked = StubBackend("google", error=SearchBlockedError("captcha"))
    backup = StubBackend("searxng", results=result())
    response = await SearchRouter([blocked, backup]).search(QUERY)
    assert response.backend == "searxng"


async def test_failover_when_the_primary_errors():
    broken = StubBackend("google", error=UpstreamError("boom"))
    backup = StubBackend("searxng", results=result())
    assert (await SearchRouter([broken, backup]).search(QUERY)).backend == "searxng"


async def test_empty_primary_falls_through_to_the_next_backend():
    empty = StubBackend("google", results=[])
    backup = StubBackend("searxng", results=result())
    assert (await SearchRouter([empty, backup]).search(QUERY)).backend == "searxng"


async def test_a_genuinely_empty_result_is_returned_by_the_last_backend():
    only = StubBackend("google", results=[])
    response = await SearchRouter([only]).search(QUERY)
    assert response.is_empty is True
    assert response.backend == "google"


async def test_no_backends_at_all_raises():
    with pytest.raises(ProviderUnavailableError, match="No search backend"):
        await SearchRouter([]).search(QUERY)


async def test_no_configured_backends_raises():
    router = SearchRouter([StubBackend("google", configured=False)])
    with pytest.raises(ProviderUnavailableError, match="No search backend"):
        await router.search(QUERY)


async def test_every_backend_failing_raises_with_the_last_error():
    router = SearchRouter(
        [
            StubBackend("google", error=SearchBlockedError("captcha served")),
            StubBackend("searxng", error=UpstreamError("searxng down")),
        ]
    )
    with pytest.raises(ProviderUnavailableError, match="searxng down"):
        await router.search(QUERY)


async def test_all_backends_empty_returns_a_genuine_no_results_answer():
    """Zero results is a real answer, not a failure.

    Each empty backend is retried against the next in case the emptiness was a
    parse failure, but once they are exhausted the caller gets an empty
    response rather than a 503 - searching for gibberish should not look like
    an outage.
    """
    a = StubBackend("a", results=[])
    b = StubBackend("b", results=[])
    response = await SearchRouter([a, b]).search(QUERY)
    assert response.is_empty is True
    assert a.calls == 1
    assert b.calls == 1
