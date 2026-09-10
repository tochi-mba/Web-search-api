import pytest

from app.core.errors import ForbiddenUrlError, TimeoutProblem, UpstreamError


async def test_scrapes_a_single_url(client, fake_pages):
    fake_pages.add("https://a.example.com/1", "The page said something useful.")
    response = await client.post(
        "/v1/scrape", json={"urls": ["https://a.example.com/1"], "summarize": False}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["status"] == "ok"
    assert result["page"]["text"] == "The page said something useful."
    assert result["page"]["title"] == "A Page"
    assert result["page"]["word_count"] == 5
    assert result["summary"] is None


async def test_scrapes_multiple_urls(client, fake_pages):
    fake_pages.add("https://a.example.com/1", "First page.")
    fake_pages.add("https://b.example.com/2", "Second page.")
    response = await client.post(
        "/v1/scrape",
        json={"urls": ["https://a.example.com/1", "https://b.example.com/2"], "summarize": False},
    )
    texts = [r["page"]["text"] for r in response.json()["results"]]
    assert texts == ["First page.", "Second page."]


async def test_summary_is_attached_per_url_by_default(client, fake_pages):
    fake_pages.add("https://a.example.com/1", "Content one.")
    fake_pages.add("https://b.example.com/2", "Content two.")
    response = await client.post(
        "/v1/scrape",
        json={"urls": ["https://a.example.com/1", "https://b.example.com/2"]},
    )
    body = response.json()
    assert body["summary"] is None
    for result in body["results"]:
        assert result["summary"]["executive_summary"] == "A tight executive summary."
        assert result["summary"]["key_points"] == ["first point", "second point"]


async def test_summarize_together_produces_one_summary(client, fake_pages):
    fake_pages.add("https://a.example.com/1", "Content one.")
    fake_pages.add("https://b.example.com/2", "Content two.")
    response = await client.post(
        "/v1/scrape",
        json={
            "urls": ["https://a.example.com/1", "https://b.example.com/2"],
            "summarize_together": True,
        },
    )
    body = response.json()
    assert body["summary"]["executive_summary"] == "A tight executive summary."
    assert all(r["summary"] is None for r in body["results"])


async def test_combined_summary_sees_every_page(client, fake_pages, fake_summarizer):
    fake_pages.add("https://a.example.com/1", "Alpha content.")
    fake_pages.add("https://b.example.com/2", "Beta content.")
    await client.post(
        "/v1/scrape",
        json={
            "urls": ["https://a.example.com/1", "https://b.example.com/2"],
            "summarize_together": True,
        },
    )
    content = fake_summarizer.calls[0]["content"]
    assert "Alpha content." in content
    assert "Beta content." in content


async def test_additional_notes_reach_the_summarizer(client, fake_pages, fake_summarizer):
    fake_pages.add("https://a.example.com/1", "Content.")
    response = await client.post(
        "/v1/scrape",
        json={"urls": ["https://a.example.com/1"], "additional_notes": "Focus on pricing."},
    )
    assert fake_summarizer.calls[0]["additional_notes"] == "Focus on pricing."
    assert response.json()["results"][0]["summary"]["notes_applied"] is True


async def test_model_selection_is_passed_through(client, fake_pages, fake_summarizer):
    fake_pages.add("https://a.example.com/1", "Content.")
    await client.post(
        "/v1/scrape", json={"urls": ["https://a.example.com/1"], "model": "openai:gpt-4o"}
    )
    assert fake_summarizer.calls[0]["model_id"] == "openai:gpt-4o"


@pytest.mark.parametrize("mode", ["auto", "always", "never"])
async def test_render_mode_is_passed_through(client, fake_pages, mode):
    fake_pages.add("https://a.example.com/1", "Content.")
    await client.post(
        "/v1/scrape",
        json={"urls": ["https://a.example.com/1"], "render_js": mode, "summarize": False},
    )
    assert fake_pages.calls[0] == ("https://a.example.com/1", mode)


# --- partial failure ------------------------------------------------------- #


async def test_one_bad_url_does_not_fail_the_batch(client, fake_pages):
    """A dead link must not cost the caller the pages that did work."""
    fake_pages.add("https://a.example.com/1", "Good content.")
    fake_pages.fail(
        "https://b.example.com/2", UpstreamError("Origin returned an error", detail="404")
    )

    response = await client.post(
        "/v1/scrape",
        json={"urls": ["https://a.example.com/1", "https://b.example.com/2"], "summarize": False},
    )
    assert response.status_code == 200
    good, bad = response.json()["results"]
    assert good["status"] == "ok"
    assert bad["status"] == "error"
    assert bad["error"]["code"] == "upstream_error"
    assert bad["page"] is None


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ForbiddenUrlError("Blocked address"), "forbidden_url_error"),
        (TimeoutProblem("Fetch timed out"), "timeout_problem"),
        (UpstreamError("Origin failed"), "upstream_error"),
    ],
)
async def test_error_codes_are_surfaced_per_item(client, fake_pages, error, code):
    fake_pages.fail("https://a.example.com/1", error)
    response = await client.post(
        "/v1/scrape", json={"urls": ["https://a.example.com/1"], "summarize": False}
    )
    assert response.json()["results"][0]["error"]["code"] == code


async def test_failed_pages_are_not_summarized(client, fake_pages, fake_summarizer):
    fake_pages.fail("https://a.example.com/1", UpstreamError("nope"))
    await client.post("/v1/scrape", json={"urls": ["https://a.example.com/1"]})
    assert fake_summarizer.calls == []


# --- validation ------------------------------------------------------------ #


async def test_empty_url_list_is_rejected(client):
    response = await client.post("/v1/scrape", json={"urls": []})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_problem"


async def test_too_many_urls_are_rejected(client):
    response = await client.post(
        "/v1/scrape", json={"urls": [f"https://a.example.com/{i}" for i in range(50)]}
    )
    assert response.status_code == 422


async def test_unknown_fields_are_rejected(client):
    response = await client.post(
        "/v1/scrape", json={"urls": ["https://a.example.com/1"], "typoed_field": True}
    )
    assert response.status_code == 422


async def test_invalid_render_mode_is_rejected(client):
    response = await client.post(
        "/v1/scrape", json={"urls": ["https://a.example.com/1"], "render_js": "sometimes"}
    )
    assert response.status_code == 422


async def test_overlong_notes_are_rejected(client):
    response = await client.post(
        "/v1/scrape",
        json={"urls": ["https://a.example.com/1"], "additional_notes": "x" * 99_999},
    )
    assert response.status_code == 422
