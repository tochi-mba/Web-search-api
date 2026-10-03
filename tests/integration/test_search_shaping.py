"""A person's language, region, blocked sites and page reading shape their searches.

The new behaviour, named: `search.language`, `search.region`, `search.blocked_domains`,
`search.read_top_pages`, `search.summary_length` and `search.research_notes` are read for the
profile a request runs as. A search that does not say takes the person's language, region
and page count; a blocked site's results never appear; and every summary is written at the
person's length with their standing notes. A request that says wins, and with nobody's
choices every search runs exactly as it did before.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

import httpx
from settings_client.testing import FakeSettingsClient

from app.api import deps
from app.services.llm.prompts import SummaryLength
from app.services.preferences import (
    Preferences,
    SettingsApiPreferences,
    deployment_preferences,
)
from app.services.search.base import SearchResponse, SearchResult
from tests.conftest import make_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

USER_TOKEN = "a-user-token-from-keyring"


def chose(**values: Any) -> Preferences:
    """The deployment's preferences, with this person's own choices over them."""
    return dataclasses.replace(deployment_preferences(make_settings()), **values)


def as_person(app: FastAPI, preferences: Preferences) -> None:
    app.dependency_overrides[deps.get_preferences] = lambda: preferences


async def search(client: httpx.AsyncClient, *queries: dict[str, Any], **body: Any) -> Any:
    response = await client.post(
        "/v1/search", json={"queries": list(queries) or [{"query": "x"}], **body}
    )
    assert response.status_code == 200
    return response.json()["results"]


def answers(fake_search: Any, *urls: str) -> None:
    fake_search.default = SearchResponse(
        query="",
        backend="google",
        results=[
            SearchResult(title=f"R{rank}", url=url, snippet=f"snippet {rank}", rank=rank)
            for rank, url in enumerate(urls, start=1)
        ],
    )


# -- reading the person's choices -----------------------------------------------------------


async def read(client: FakeSettingsClient) -> Preferences:
    source = SettingsApiPreferences(
        client=client, settings=make_settings(keyring_default_profile="personal")
    )
    return await source.for_token(USER_TOKEN)


async def test_a_persons_choices_are_read_for_their_profile() -> None:
    client = FakeSettingsClient()
    client.seed(
        "search",
        {
            "language": "fr",
            "region": "ca",
            "read_top_pages": 4,
            "summary_length": "brief",
            "research_notes": "Prefer primary sources.",
        },
        profile="personal",
    )
    client.seed("search", {"blocked_domains": ["Pinterest.com ", "farm.example"]})

    preferences = await read(client)

    assert client.asked == [("search", None), ("search", "personal")]
    assert (preferences.language, preferences.region) == ("fr", "ca")
    assert preferences.blocked_domains == ("pinterest.com", "farm.example")
    assert preferences.read_top_pages == 4
    assert preferences.summary_length is SummaryLength.BRIEF
    assert preferences.research_notes == "Prefer primary sources."


async def test_a_person_who_chose_nothing_searches_as_before() -> None:
    preferences = await read(FakeSettingsClient())

    assert (preferences.language, preferences.region) == (None, None)
    assert preferences.blocked_domains == ()
    assert preferences.read_top_pages == 0
    assert preferences.summary_length is SummaryLength.STANDARD
    assert preferences.research_notes is None


async def test_values_settings_api_should_never_send_change_nothing() -> None:
    client = FakeSettingsClient()
    client.seed(
        "search",
        {
            "language": 7,
            "region": "",
            "blocked_domains": "pinterest.com",
            "read_top_pages": -1,
            "summary_length": "epic",
            "research_notes": ["not", "text"],
        },
    )

    preferences = await read(client)

    assert (preferences.language, preferences.region) == (None, None)
    assert preferences.blocked_domains == ()
    assert preferences.read_top_pages == 0
    assert preferences.summary_length is SummaryLength.STANDARD
    assert preferences.research_notes is None


async def test_no_more_pages_are_read_than_a_request_could_ask_for() -> None:
    client = FakeSettingsClient()
    client.seed("search", {"read_top_pages": 50})

    assert (await read(client)).read_top_pages == 10


async def test_an_outage_searches_as_before() -> None:
    client = FakeSettingsClient()
    client.unavailable = True

    preferences = await read(client)

    assert (preferences.language, preferences.region) == (None, None)
    assert preferences.blocked_domains == ()
    assert preferences.read_top_pages == 0
    assert preferences.summary_length is SummaryLength.STANDARD
    assert preferences.research_notes is None


# -- language and region --------------------------------------------------------------------


async def test_a_request_that_says_nothing_takes_the_persons_language_and_region(
    app, client, fake_search
):
    as_person(app, chose(language="fr", region="ca"))

    await search(client, summarize=False)
    await search(client, summarize=False, language="de", region="at")

    first, second = fake_search.queries
    assert (first.language, first.region) == ("fr", "ca")
    assert (second.language, second.region) == ("de", "at")


async def test_with_nobodys_choices_results_are_english_and_ranked_for_the_us(client, fake_search):
    await search(client, summarize=False)

    sent = fake_search.queries[0]
    assert (sent.language, sent.region) == ("en", "us")


# -- blocked sites --------------------------------------------------------------------------


async def test_a_blocked_sites_results_and_its_subdomains_never_appear(app, client, fake_search):
    as_person(app, chose(blocked_domains=("pinterest.com",)))
    answers(
        fake_search,
        "https://pinterest.com/pin/1",
        "https://www.Pinterest.com/pin/2",
        "https://notpinterest.com/3",
        "http://[::1/malformed",
        "https://a.example.com/5",
    )

    (result,) = await search(client, summarize=False)

    assert [(item["url"], item["rank"]) for item in result["results"]] == [
        ("https://notpinterest.com/3", 3),
        ("http://[::1/malformed", 4),
        ("https://a.example.com/5", 5),
    ]


async def test_a_query_that_names_a_blocked_site_gets_its_results(app, client, fake_search):
    as_person(app, chose(blocked_domains=("pinterest.com", "ads.example.com")))
    answers(fake_search, "https://www.pinterest.com/pin/1", "https://ads.example.com/2")

    (result,) = await search(client, {"query": "x", "site": "WWW.pinterest.com"}, summarize=False)

    assert [item["url"] for item in result["results"]] == ["https://www.pinterest.com/pin/1"]


async def test_a_blocked_sites_page_is_never_read(app, client, fake_search, fake_pages):
    as_person(app, chose(blocked_domains=("farm.example",), read_top_pages=2))
    answers(fake_search, "https://farm.example/1", "https://a.example.com/2")

    await search(client)

    assert [url for url, _ in fake_pages.calls] == ["https://a.example.com/2"]


async def test_a_page_named_to_scrape_is_read_even_on_a_blocked_site(app, client, fake_pages):
    """Blocking a site hides it from results; a URL the request names was asked for, and wins."""
    as_person(app, chose(blocked_domains=("farm.example",)))

    response = await client.post(
        "/v1/scrape", json={"urls": ["https://farm.example/1"], "summarize": False}
    )

    assert response.json()["results"][0]["status"] == "ok"
    assert [url for url, _ in fake_pages.calls] == ["https://farm.example/1"]


# -- reading the top pages ------------------------------------------------------------------


async def test_a_summarised_search_that_says_nothing_reads_the_persons_page_count(
    app, client, fake_search, fake_pages, fake_summarizer
):
    as_person(app, chose(read_top_pages=1))
    fake_pages.add("https://a.example.com/1", "The full text of the first page.")

    (result,) = await search(client)

    assert [url for url, _ in fake_pages.calls] == ["https://a.example.com/1"]
    assert result["results"][0]["content"] == "The full text of the first page."
    assert "The full text of the first page." in fake_summarizer.calls[0]["content"]


async def test_a_request_that_names_its_pages_wins(app, client, fake_pages):
    as_person(app, chose(read_top_pages=1))

    await search(client, max_pages=2)
    assert len(fake_pages.calls) == 2

    fake_pages.calls.clear()
    await search(client, fetch_pages=True)
    assert len(fake_pages.calls) == 1

    fake_pages.calls.clear()
    await search(client, fetch_pages=False)
    assert fake_pages.calls == []


async def test_a_search_with_no_summary_reads_no_pages_it_did_not_ask_for(app, client, fake_pages):
    as_person(app, chose(read_top_pages=2))

    await search(client, summarize=False)

    assert fake_pages.calls == []


async def test_with_nobodys_choices_pages_are_read_only_when_asked_and_three_at_most(
    client, fake_search, fake_pages
):
    answers(fake_search, *(f"https://a.example.com/{n}" for n in range(1, 6)))

    await search(client)
    assert fake_pages.calls == []

    await search(client, fetch_pages=True, summarize=False)
    assert len(fake_pages.calls) == 3
