"""A summary that fails costs only the summary.

The bug, named: the model that writes a summary is a dependency the results do not have, and a
timeout, a 502 or a provider's 400 from it failed the whole response. A search that found its
results reported none; a page that was fetched reported that it could not be.
"""

from app.core.errors import TimeoutProblem, UpstreamError


async def test_a_search_keeps_its_results_when_the_summary_fails(client, fake_summarizer):
    fake_summarizer.error = TimeoutProblem("The model did not answer in time")
    response = await client.post("/v1/search", json={"queries": [{"query": "widget latency"}]})

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "ok"
    assert len(result["results"]) == 2
    assert result["summary"] is None
    assert result["summary_error"]["code"] == "timeout_problem"


async def test_a_fetched_page_keeps_its_text_when_its_summary_fails(
    client, fake_pages, fake_summarizer
):
    fake_pages.add("https://a.example.com/1", "The page said something useful.")
    fake_summarizer.error = UpstreamError("Provider exploded", detail="502 from provider")
    response = await client.post("/v1/scrape", json={"urls": ["https://a.example.com/1"]})

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "ok"
    assert result["page"]["text"] == "The page said something useful."
    assert result["summary"] is None
    assert result["summary_error"]["code"] == "upstream_error"


async def test_pages_summarised_together_keep_their_text_when_the_summary_fails(
    client, fake_pages, fake_summarizer
):
    fake_pages.add("https://a.example.com/1", "First page.")
    fake_pages.add("https://b.example.com/2", "Second page.")
    fake_summarizer.error = UpstreamError("Provider exploded")
    response = await client.post(
        "/v1/scrape",
        json={
            "urls": ["https://a.example.com/1", "https://b.example.com/2"],
            "summarize_together": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [r["page"]["text"] for r in body["results"]] == ["First page.", "Second page."]
    assert body["summary"] is None
    assert body["summary_error"]["code"] == "upstream_error"


async def test_a_summary_that_works_carries_no_error(client):
    response = await client.post("/v1/search", json={"queries": [{"query": "widget latency"}]})
    result = response.json()["results"][0]
    assert result["summary"]["executive_summary"]
    assert result["summary_error"] is None
