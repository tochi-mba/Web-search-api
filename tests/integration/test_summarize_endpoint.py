async def test_summarises_supplied_text(client):
    response = await client.post(
        "/v1/summarize", json={"text": "A long piece of text the caller already has."}
    )
    assert response.status_code == 200
    summary = response.json()["summary"]
    assert summary["executive_summary"] == "A tight executive summary."
    assert summary["key_points"] == ["first point", "second point"]
    assert summary["model"] == "anthropic:claude-opus-5"


async def test_usage_and_truncation_are_reported(client):
    body = (await client.post("/v1/summarize", json={"text": "Some text."})).json()
    summary = body["summary"]
    assert summary["usage"] == {"input_tokens": 400, "output_tokens": 50}
    assert summary["truncated"] is False
    assert summary["chars_submitted"] == len("Some text.")


async def test_options_reach_the_summarizer(client, fake_summarizer):
    await client.post(
        "/v1/summarize",
        json={
            "text": "Some text.",
            "model": "openai:gpt-4o",
            "topic": "widget latency",
            "additional_notes": "Focus on cost.",
            "sources": ["https://a.example.com/1"],
        },
    )
    call = fake_summarizer.calls[0]
    assert call["model_id"] == "openai:gpt-4o"
    assert call["topic"] == "widget latency"
    assert call["additional_notes"] == "Focus on cost."
    assert call["sources"] == ["https://a.example.com/1"]


async def test_empty_sources_are_passed_as_none(client, fake_summarizer):
    await client.post("/v1/summarize", json={"text": "Some text."})
    assert fake_summarizer.calls[0]["sources"] is None


async def test_empty_text_is_rejected(client):
    assert (await client.post("/v1/summarize", json={"text": ""})).status_code == 422


async def test_missing_text_is_rejected(client):
    assert (await client.post("/v1/summarize", json={})).status_code == 422


async def test_unknown_model_surfaces_as_a_problem(client, fake_summarizer):
    from app.core.errors import NotFoundError

    fake_summarizer.error = NotFoundError("Unknown or unavailable model", detail="nope")
    response = await client.post("/v1/summarize", json={"text": "Some text.", "model": "nope:nope"})
    assert response.status_code == 404
    assert response.json()["code"] == "not_found_error"
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_no_provider_available_surfaces_as_503(client, fake_summarizer):
    from app.core.errors import ProviderUnavailableError

    fake_summarizer.error = ProviderUnavailableError("No LLM provider is currently available")
    response = await client.post("/v1/summarize", json={"text": "Some text."})
    assert response.status_code == 503
