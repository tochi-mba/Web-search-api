"""A person's choices are read for the profile the request runs as.

The bug, named: web-search-api asked settings-api for the ``search`` namespace without naming
a profile. Its model, backend, safe-search level, result count and recency are profile-scoped,
and settings-api returns a profile's values only to a resolve that names it, so each reached
this service as the catalogue default: a person who chose ``strict`` filtering got
``moderate``. The shared test fake ignored the profile too, so no test noticed.
"""

from __future__ import annotations

from settings_client import Fallback, OnUnavailable
from settings_client.testing import FakeSettingsClient

from app.services.preferences import SettingsApiPreferences
from app.services.search.base import SafeSearch
from tests.conftest import make_settings

TOKEN = "a-user-token-from-keyring"


def reading(client: FakeSettingsClient) -> SettingsApiPreferences:
    return SettingsApiPreferences(
        client=client, settings=make_settings(keyring_default_profile="personal")
    )


def chosen_in(profile: str) -> FakeSettingsClient:
    client = FakeSettingsClient()
    client.seed(
        "search",
        {"safe_search": "strict", "recency_days": 7, "search_backend": "searxng"},
        profile=profile,
    )
    return client


async def test_a_named_profile_is_the_one_asked_for_and_its_choices_apply() -> None:
    client = chosen_in("family")

    preferences = await reading(client).for_token(TOKEN, profile="family")

    assert client.asked == [("search", "family")]
    assert preferences.safe_search is SafeSearch.STRICT
    assert preferences.recency_days == 7
    assert preferences.search_backend == "searxng"


async def test_with_no_profile_named_the_persons_default_profile_is_asked_for() -> None:
    client = chosen_in("family")
    client.seed("search", {"default_profile": "family"})

    preferences = await reading(client).for_token(TOKEN)

    assert client.asked == [("search", None), ("search", "family")]
    assert preferences.safe_search is SafeSearch.STRICT


async def test_with_no_default_chosen_the_deployments_default_profile_is_asked_for() -> None:
    client = chosen_in("personal")

    preferences = await reading(client).for_token(TOKEN)

    assert client.asked == [("search", None), ("search", "personal")]
    assert preferences.recency_days == 7


async def test_another_profiles_choices_do_not_apply() -> None:
    client = chosen_in("family")

    preferences = await reading(client).for_token(TOKEN, profile="work")

    assert preferences.safe_search is SafeSearch.MODERATE
    assert preferences.recency_days is None


async def test_a_default_profile_that_cannot_be_read_asks_nothing_more() -> None:
    client = FakeSettingsClient()
    client.unavailable = True
    client.seed_fallback(
        "search",
        "default_profile",
        Fallback(default="personal", on_unavailable=OnUnavailable.REFUSE),
    )

    preferences = await reading(client).for_token(TOKEN)

    assert client.asked == [("search", None)]
    assert preferences.default_profile is None
