from app.core.errors import ProviderUnavailableError, SearchBlockedError
from app.services.search.base import SearchResponse


async def test_runs_a_single_query(client, fake_search):
    response = await client.post(
        "/v1/search", json={"queries": [{"query": "widget latency"}], "summarize": False}
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "ok"
    assert result["query"] == "widget latency"
    assert result["backend"] == "google"
    assert [r["url"] for r in result["results"]] == [
        "https://a.example.com/1",
        "https://b.example.com/2",
    ]


async def test_runs_a_batch_of_queries(client):
    response = await client.post(
        "/v1/search",
        json={
            "queries": [{"query": "one"}, {"query": "two"}, {"query": "three"}],
            "summarize": False,
        },
    )
    assert [r["query"] for r in response.json()["results"]] == ["one", "two", "three"]


async def test_query_options_reach_the_backend(client, fake_search):
    await client.post(
        "/v1/search",
        json={
            "queries": [{"query": "best tv", "max_results": 5, "site": "reddit.com"}],
            "language": "fr",
            "region": "ca",
            "safe_search": False,
            "summarize": False,
        },
    )
    sent = fake_search.queries[0]
    assert sent.max_results == 5
    assert sent.site == "reddit.com"
    assert sent.language == "fr"
    assert sent.region == "ca"
    assert sent.safe_search is False


# --- summarising ----------------------------------------------------------- #


async def test_summary_is_attached_by_default(client):
    response = await client.post("/v1/search", json={"queries": [{"query": "x"}]})
    summary = response.json()["results"][0]["summary"]
    assert summary["executive_summary"] == "A tight executive summary."
    assert summary["key_points"] == ["first point", "second point"]


async def test_summary_uses_snippets_when_pages_are_not_fetched(client, fake_summarizer):
    await client.post("/v1/search", json={"queries": [{"query": "x"}]})
    content = fake_summarizer.calls[0]["content"]
    assert "First snippet." in content
    assert "Second snippet." in content


async def test_query_is_passed_as_the_topic(client, fake_summarizer):
    await client.post("/v1/search", json={"queries": [{"query": "widget latency"}]})
    assert fake_summarizer.calls[0]["topic"] == "widget latency"


async def test_result_urls_are_passed_as_sources(client, fake_summarizer):
    await client.post("/v1/search", json={"queries": [{"query": "x"}]})
    assert fake_summarizer.calls[0]["sources"] == [
        "https://a.example.com/1",
        "https://b.example.com/2",
    ]


async def test_batch_notes_are_applied(client, fake_summarizer):
    await client.post(
        "/v1/search",
        json={"queries": [{"query": "x"}], "additional_notes": "Focus on price."},
    )
    assert fake_summarizer.calls[0]["additional_notes"] == "Focus on price."


async def test_per_query_notes_override_batch_notes(client, fake_summarizer):
    await client.post(
        "/v1/search",
        json={
            "queries": [{"query": "x", "additional_notes": "Query-specific."}],
            "additional_notes": "Batch-wide.",
        },
    )
    assert fake_summarizer.calls[0]["additional_notes"] == "Query-specific."


async def test_summarize_false_skips_the_model(client, fake_summarizer):
    await client.post("/v1/search", json={"queries": [{"query": "x"}], "summarize": False})
    assert fake_summarizer.calls == []


async def test_empty_results_are_not_summarized(client, fake_search, fake_summarizer):
    fake_search.responses["nothing"] = SearchResponse(query="nothing", backend="google", results=[])
    await client.post("/v1/search", json={"queries": [{"query": "nothing"}]})
    assert fake_summarizer.calls == []


# --- deep page fetching ---------------------------------------------------- #


async def test_fetch_pages_scrapes_result_pages(client, fake_pages):
    fake_pages.add("https://a.example.com/1", "The full text of the first page.")
    fake_pages.add("https://b.example.com/2", "The full text of the second page.")
    response = await client.post(
        "/v1/search",
        json={"queries": [{"query": "x"}], "fetch_pages": True, "summarize": False},
    )
    contents = [r["content"] for r in response.json()["results"][0]["results"]]
    assert contents == [
        "The full text of the first page.",
        "The full text of the second page.",
    ]


async def test_fetch_pages_feeds_full_text_to_the_summarizer(client, fake_pages, fake_summarizer):
    fake_pages.add("https://a.example.com/1", "Deep content from page one.")
    fake_pages.add("https://b.example.com/2", "Deep content from page two.")
    await client.post("/v1/search", json={"queries": [{"query": "x"}], "fetch_pages": True})
    content = fake_summarizer.calls[0]["content"]
    assert "Deep content from page one." in content


async def test_max_pages_bounds_the_scraping(client, fake_pages):
    await client.post(
        "/v1/search",
        json={"queries": [{"query": "x"}], "fetch_pages": True, "max_pages": 1, "summarize": False},
    )
    assert len(fake_pages.calls) == 1


async def test_pages_are_not_fetched_by_default(client, fake_pages):
    await client.post("/v1/search", json={"queries": [{"query": "x"}], "summarize": False})
    assert fake_pages.calls == []


async def test_a_dead_link_keeps_its_snippet(client, fake_pages):
    """Deep fetching is enrichment; one unreachable page must not lose the result."""
    from app.core.errors import UpstreamError

    fake_pages.fail("https://a.example.com/1", UpstreamError("404"))
    fake_pages.add("https://b.example.com/2", "Second page text.")
    response = await client.post(
        "/v1/search",
        json={"queries": [{"query": "x"}], "fetch_pages": True, "summarize": False},
    )
    results = response.json()["results"][0]["results"]
    assert results[0]["content"] is None
    assert results[0]["snippet"] == "First snippet."
    assert results[1]["content"] == "Second page text."


async def test_no_results_means_nothing_to_fetch(client, fake_search, fake_pages):
    fake_search.responses["nothing"] = SearchResponse(query="nothing", backend="google", results=[])
    await client.post(
        "/v1/search",
        json={"queries": [{"query": "nothing"}], "fetch_pages": True, "summarize": False},
    )
    assert fake_pages.calls == []


# --- partial failure ------------------------------------------------------- #


async def test_one_failing_query_does_not_fail_the_batch(client, fake_search):
    fake_search.errors["blocked"] = SearchBlockedError(
        "Google served a captcha page", detail="interstitial"
    )
    response = await client.post(
        "/v1/search",
        json={"queries": [{"query": "fine"}, {"query": "blocked"}], "summarize": False},
    )
    assert response.status_code == 200
    fine, blocked = response.json()["results"]
    assert fine["status"] == "ok"
    assert blocked["status"] == "error"
    assert blocked["error"]["code"] == "search_blocked_error"
    assert blocked["results"] == []


async def test_all_backends_down_is_reported_per_query(client, fake_search):
    fake_search.errors["x"] = ProviderUnavailableError("Every search backend failed")
    response = await client.post("/v1/search", json={"queries": [{"query": "x"}]})
    assert response.status_code == 200
    assert response.json()["results"][0]["error"]["code"] == "provider_unavailable_error"


# --- validation ------------------------------------------------------------ #


async def test_empty_query_list_is_rejected(client):
    assert (await client.post("/v1/search", json={"queries": []})).status_code == 422


async def test_blank_query_is_rejected(client):
    response = await client.post("/v1/search", json={"queries": [{"query": "   "}]})
    assert response.status_code == 422


async def test_too_many_queries_are_rejected(client):
    response = await client.post(
        "/v1/search", json={"queries": [{"query": f"q{i}"} for i in range(50)]}
    )
    assert response.status_code == 422


async def test_max_results_is_bounded(client):
    response = await client.post(
        "/v1/search", json={"queries": [{"query": "x", "max_results": 9999}]}
    )
    assert response.status_code == 422


async def test_unknown_field_is_rejected(client):
    response = await client.post("/v1/search", json={"queries": [{"query": "x"}], "nonsense": 1})
    assert response.status_code == 422


async def test_query_is_trimmed(client, fake_search):
    await client.post("/v1/search", json={"queries": [{"query": "  spaced  "}], "summarize": False})
    assert fake_search.queries[0].query == "spaced"


async def test_results_carry_rank_and_title(client):
    response = await client.post(
        "/v1/search", json={"queries": [{"query": "x"}], "summarize": False}
    )
    first = response.json()["results"][0]["results"][0]
    assert first["rank"] == 1
    assert first["title"] == "Result One"
