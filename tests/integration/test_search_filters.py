"""A person's safe-search level and recency reach the search backend.

The bug, named: `search.safe_search` and `search.recency_days` could be set and read back,
and changed nothing. Every search ran with whatever the request body said, and a body that
said nothing got the on/off default, never the person's own choice.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from settings_client.testing import FakeSettingsClient

from app.api import deps
from app.services.preferences import (
    Preferences,
    SettingsApiPreferences,
    deployment_preferences,
)
from app.services.search.base import SafeSearch, SearchQuery
from app.services.search.google import build_search_url
from app.services.search.searxng import SearxngSearchBackend, time_range
from tests.conftest import make_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

OFF, MODERATE, STRICT = SafeSearch.OFF, SafeSearch.MODERATE, SafeSearch.STRICT
USER_TOKEN = "a-user-token-from-keyring"


def chose(**values: Any) -> Preferences:
    """The deployment's preferences, with this person's own choices over them."""
    return dataclasses.replace(deployment_preferences(make_settings()), **values)


def as_person(app: FastAPI, preferences: Preferences) -> None:
    app.dependency_overrides[deps.get_preferences] = lambda: preferences


async def search(client: httpx.AsyncClient, **body: Any) -> None:
    response = await client.post(
        "/v1/search", json={"queries": [{"query": "x"}], "summarize": False, **body}
    )
    assert response.status_code == 200


# -- the rule -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "chosen", "expected"),
    [
        # Nobody has chosen: the request alone decides, as it always did.
        (None, None, MODERATE),
        (True, None, MODERATE),
        (False, None, OFF),
        # A request that says nothing takes the person's level.
        (None, OFF, OFF),
        (None, MODERATE, MODERATE),
        (None, STRICT, STRICT),
        # A request that says can ask for more filtering, never less.
        (True, OFF, MODERATE),
        (True, STRICT, STRICT),
        (False, OFF, OFF),
        (False, MODERATE, MODERATE),
        (False, STRICT, STRICT),
    ],
)
def test_the_stricter_of_the_request_and_the_person_wins(
    requested: bool | None, chosen: SafeSearch | None, expected: SafeSearch
) -> None:
    assert SafeSearch.for_request(requested, chosen=chosen) is expected


def test_levels_are_ordered_least_to_most() -> None:
    assert OFF.at_least(STRICT) is STRICT
    assert STRICT.at_least(OFF) is STRICT
    assert MODERATE.at_least(MODERATE) is MODERATE


# -- what each backend is sent --------------------------------------------------------------


def test_google_has_one_filter_and_a_day_count() -> None:
    plain = build_search_url(SearchQuery(query="x", safe_search=OFF))
    assert "safe=" not in plain
    assert "tbs=" not in plain

    assert "tbs=qdr%3Ad&" in build_search_url(SearchQuery(query="x", recency_days=1)) + "&"
    assert "tbs=qdr%3Ad7&" in build_search_url(SearchQuery(query="x", recency_days=7)) + "&"


@pytest.mark.parametrize(
    ("days", "name"),
    [
        (1, "day"),
        (2, "week"),
        (7, "week"),
        (8, "month"),
        (31, "month"),
        (32, "year"),
        (365, "year"),
    ],
)
def test_searxng_takes_the_smallest_range_that_covers_the_days(days: int, name: str) -> None:
    assert time_range(days) == name


@respx.mock
@pytest.mark.parametrize(("level", "sent"), [(OFF, "0"), (MODERATE, "1"), (STRICT, "2")])
async def test_searxng_is_sent_all_three_levels(level: SafeSearch, sent: str) -> None:
    route = respx.get("https://s.test/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    async with httpx.AsyncClient() as client:
        backend = SearxngSearchBackend(client, base_url="https://s.test")
        await backend.search(SearchQuery(query="x", safe_search=level))
        await backend.search(SearchQuery(query="x", safe_search=level, recency_days=3))

    first, second = (call.request.url.params for call in route.calls)
    assert first["safesearch"] == sent
    assert "time_range" not in first
    assert second["time_range"] == "week"


# -- reading the person's choices -----------------------------------------------------------


async def read(values: dict[str, Any]) -> Preferences:
    client = FakeSettingsClient()
    client.seed("search", values)
    source = SettingsApiPreferences(client=client, settings=make_settings())
    return await source.for_token(USER_TOKEN)


async def test_a_persons_level_and_recency_are_read() -> None:
    preferences = await read({"safe_search": "strict", "recency_days": 30})

    assert preferences.safe_search is STRICT
    assert preferences.recency_days == 30


async def test_a_person_who_chose_nothing_filters_moderately_and_by_no_date() -> None:
    preferences = await read({})

    assert preferences.safe_search is MODERATE
    assert preferences.recency_days is None


async def test_a_value_settings_api_should_never_send_is_the_middle_level() -> None:
    preferences = await read({"safe_search": "sometimes", "recency_days": "soon"})

    assert preferences.safe_search is MODERATE
    assert preferences.recency_days is None


async def test_an_outage_cannot_turn_filtering_off() -> None:
    client = FakeSettingsClient()
    client.unavailable = True
    source = SettingsApiPreferences(client=client, settings=make_settings())

    preferences = await source.for_token(USER_TOKEN)

    assert preferences.safe_search is MODERATE
    assert preferences.recency_days is None


def test_without_settings_api_nobody_has_chosen() -> None:
    preferences = deployment_preferences(make_settings())

    assert preferences.safe_search is None
    assert preferences.recency_days is None


# -- over HTTP ------------------------------------------------------------------------------


async def test_a_request_that_says_nothing_takes_the_persons_choices(app, client, fake_search):
    as_person(app, chose(safe_search=STRICT, recency_days=14))

    await search(client)

    sent = fake_search.queries[0]
    assert sent.safe_search is STRICT
    assert sent.recency_days == 14


async def test_a_request_cannot_filter_less_than_the_person_chose(app, client, fake_search):
    as_person(app, chose(safe_search=STRICT))

    await search(client, safe_search=False)

    assert fake_search.queries[0].safe_search is STRICT


async def test_a_request_names_its_own_recency(app, client, fake_search):
    as_person(app, chose(recency_days=14))

    await search(client, recency_days=2)

    assert fake_search.queries[0].recency_days == 2


async def test_with_nobodys_choices_the_request_decides_as_before(client, fake_search):
    await search(client)
    await search(client, safe_search=False, recency_days=7)

    first, second = fake_search.queries
    assert (first.safe_search, first.recency_days) == (MODERATE, None)
    assert (second.safe_search, second.recency_days) == (OFF, 7)


@pytest.mark.parametrize("days", [0, 366, -1])
async def test_a_recency_outside_a_year_is_refused(client, days: int) -> None:
    response = await client.post(
        "/v1/search", json={"queries": [{"query": "x"}], "recency_days": days}
    )

    assert response.status_code == 422
