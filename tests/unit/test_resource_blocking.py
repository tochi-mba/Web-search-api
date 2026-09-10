"""The Playwright route handler, tested directly.

Running it through a real browser makes coverage dependent on which
sub-resources a fixture page happens to request; calling it with stand-ins is
deterministic and covers both branches.
"""

import pytest

from app.services.fetch.browser import _BLOCKED_RESOURCES, _block_heavy_resources


class FakeRoute:
    def __init__(self):
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class FakeRequest:
    def __init__(self, resource_type):
        self._resource_type = resource_type

    @property
    def resource_type(self):
        return self._resource_type


@pytest.mark.parametrize("resource_type", sorted(_BLOCKED_RESOURCES))
async def test_heavy_resources_are_aborted(resource_type):
    route = FakeRoute()
    await _block_heavy_resources(route, FakeRequest(resource_type))
    assert route.aborted is True
    assert route.continued is False


@pytest.mark.parametrize("resource_type", ["document", "xhr", "fetch", "script"])
async def test_content_resources_are_allowed(resource_type):
    route = FakeRoute()
    await _block_heavy_resources(route, FakeRequest(resource_type))
    assert route.continued is True
    assert route.aborted is False
