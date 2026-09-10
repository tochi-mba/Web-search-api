"""Background job mode.

The headline test runs the same request body both ways and asserts the results
are identical. That is what pins the synchronous and background paths together:
if they ever diverge, it fails.
"""

import asyncio

import pytest

from app.core.errors import UpstreamError

SCRAPE_BODY = {
    "urls": ["https://a.example.com/1", "https://b.example.com/2"],
    "summarize": True,
}


async def wait_for_terminal(client, job_id, *, tries=200):
    """Poll until the job reaches a terminal state."""
    for _ in range(tries):
        body = (await client.get(f"/v1/jobs/{job_id}")).json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            return body
        await asyncio.sleep(0)
    raise AssertionError(f"job {job_id} never finished")


@pytest.fixture
def seeded_pages(fake_pages):
    fake_pages.add("https://a.example.com/1", "Content of the first page.")
    fake_pages.add("https://b.example.com/2", "Content of the second page.")
    return fake_pages


# --- the equivalence guarantee --------------------------------------------- #


async def test_background_result_is_identical_to_the_sync_response(client, seeded_pages):
    """Both modes must run the same code and produce the same bytes."""
    sync = (await client.post("/v1/scrape", json=SCRAPE_BODY)).json()

    accepted = await client.post("/v1/scrape", json={**SCRAPE_BODY, "async": True})
    job = await wait_for_terminal(client, accepted.json()["job_id"])

    assert job["status"] == "succeeded"
    assert job["result"] == sync


async def test_equivalence_holds_for_search(client, fake_search):
    body = {"queries": [{"query": "widget latency"}], "summarize": True}
    sync = (await client.post("/v1/search", json=body)).json()

    accepted = await client.post("/v1/search", json={**body, "async": True})
    job = await wait_for_terminal(client, accepted.json()["job_id"])
    assert job["result"] == sync


async def test_equivalence_holds_for_summarize(client):
    body = {"text": "Some text the caller already has."}
    sync = (await client.post("/v1/summarize", json=body)).json()

    accepted = await client.post("/v1/summarize", json={**body, "async": True})
    job = await wait_for_terminal(client, accepted.json()["job_id"])
    assert job["result"] == sync


# --- the 202 envelope ------------------------------------------------------ #


async def test_submitting_returns_202_with_a_job_envelope(client, seeded_pages):
    response = await client.post("/v1/scrape", json={**SCRAPE_BODY, "async": True})
    assert response.status_code == 202

    body = response.json()
    assert body["kind"] == "scrape"
    assert body["status"] in ("queued", "running")
    assert body["poll_url"] == f"/v1/jobs/{body['job_id']}"
    assert body["created_at"] > 0


async def test_location_and_retry_after_headers_are_set(client, seeded_pages):
    response = await client.post("/v1/scrape", json={**SCRAPE_BODY, "async": True})
    assert response.headers["location"] == response.json()["poll_url"]
    assert int(response.headers["retry-after"]) > 0


async def test_the_background_field_name_also_works(client, seeded_pages):
    response = await client.post("/v1/scrape", json={**SCRAPE_BODY, "background": True})
    assert response.status_code == 202


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/scrape", {"urls": ["https://a.example.com/1"]}),
        ("/v1/search", {"queries": [{"query": "x"}]}),
        ("/v1/summarize", {"text": "some text"}),
    ],
)
async def test_every_endpoint_supports_background_mode(client, path, body):
    response = await client.post(path, json={**body, "async": True})
    assert response.status_code == 202
    assert response.json()["poll_url"].startswith("/v1/jobs/")


async def test_omitting_the_flag_still_runs_synchronously(client, seeded_pages):
    response = await client.post("/v1/scrape", json=SCRAPE_BODY)
    assert response.status_code == 200
    assert "results" in response.json()


async def test_submitting_does_not_wait_for_the_work(client, fake_pages):
    """The point of the feature: the connection is released immediately."""
    gate = asyncio.Event()
    original = fake_pages.fetch

    async def blocking(url, *, render="auto"):
        await gate.wait()
        return await original(url, render=render)

    fake_pages.fetch = blocking

    response = await client.post("/v1/scrape", json={**SCRAPE_BODY, "async": True})
    assert response.status_code == 202

    job = (await client.get(f"/v1/jobs/{response.json()['job_id']}")).json()
    assert job["status"] in ("queued", "running")
    assert job["result"] is None

    gate.set()
    await wait_for_terminal(client, response.json()["job_id"])


# --- polling --------------------------------------------------------------- #


async def test_polling_reports_timings_once_finished(client, seeded_pages):
    accepted = await client.post("/v1/scrape", json={**SCRAPE_BODY, "async": True})
    job = await wait_for_terminal(client, accepted.json()["job_id"])

    assert job["started_at"] is not None
    assert job["finished_at"] is not None
    assert job["duration_seconds"] is not None
    assert job["error"] is None


async def test_retry_after_is_sent_while_running(client, fake_pages):
    gate = asyncio.Event()
    original = fake_pages.fetch

    async def blocking(url, *, render="auto"):
        await gate.wait()
        return await original(url, render=render)

    fake_pages.fetch = blocking

    accepted = await client.post("/v1/scrape", json={**SCRAPE_BODY, "async": True})
    job_id = accepted.json()["job_id"]

    running = await client.get(f"/v1/jobs/{job_id}")
    assert "retry-after" in running.headers

    gate.set()
    await wait_for_terminal(client, job_id)
    assert "retry-after" not in (await client.get(f"/v1/jobs/{job_id}")).headers


async def test_unknown_job_returns_a_problem_document(client):
    response = await client.get("/v1/jobs/does-not-exist")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found_error"
    assert response.headers["content-type"].startswith("application/problem+json")


# --- failure --------------------------------------------------------------- #


async def test_a_failing_job_records_a_structured_error(client, fake_summarizer):
    fake_summarizer.error = UpstreamError("Provider exploded", detail="502 from provider")

    accepted = await client.post("/v1/summarize", json={"text": "x", "async": True})
    job = await wait_for_terminal(client, accepted.json()["job_id"])

    assert job["status"] == "failed"
    assert job["error"] == {
        "code": "upstream_error",
        "title": "Provider exploded",
        "detail": "502 from provider",
    }
    assert job["result"] is None


async def test_an_unexpected_crash_does_not_leak_internals(client, fake_summarizer):
    fake_summarizer.error = RuntimeError("secret connection string")

    accepted = await client.post("/v1/summarize", json={"text": "x", "async": True})
    job = await wait_for_terminal(client, accepted.json()["job_id"])

    assert job["status"] == "failed"
    assert job["error"]["code"] == "internal_error"
    assert "secret" not in str(job)


async def test_a_partial_batch_failure_still_succeeds_as_a_job(client, fake_pages):
    """Per-item errors are results, not job failures."""
    fake_pages.add("https://a.example.com/1", "Good content.")
    fake_pages.fail("https://b.example.com/2", UpstreamError("404"))

    accepted = await client.post("/v1/scrape", json={**SCRAPE_BODY, "async": True})
    job = await wait_for_terminal(client, accepted.json()["job_id"])

    assert job["status"] == "succeeded"
    statuses = [r["status"] for r in job["result"]["results"]]
    assert statuses == ["ok", "error"]


# --- cancellation ---------------------------------------------------------- #


async def test_cancelling_a_running_job(client, fake_pages):
    gate = asyncio.Event()

    async def blocking(url, *, render="auto"):
        await gate.wait()
        raise AssertionError("should have been cancelled")

    fake_pages.fetch = blocking

    accepted = await client.post("/v1/scrape", json={**SCRAPE_BODY, "async": True})
    job_id = accepted.json()["job_id"]

    cancelled = await client.delete(f"/v1/jobs/{job_id}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert (await client.get(f"/v1/jobs/{job_id}")).json()["status"] == "cancelled"


async def test_cancelling_a_finished_job_is_a_safe_no_op(client, seeded_pages):
    accepted = await client.post("/v1/scrape", json={**SCRAPE_BODY, "async": True})
    job_id = accepted.json()["job_id"]
    await wait_for_terminal(client, job_id)

    response = await client.delete(f"/v1/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


async def test_cancelling_an_unknown_job_is_a_404(client):
    assert (await client.delete("/v1/jobs/nope")).status_code == 404


# --- listing --------------------------------------------------------------- #


async def test_listing_returns_recent_jobs_newest_first(client, seeded_pages):
    first = await client.post("/v1/scrape", json={**SCRAPE_BODY, "async": True})
    second = await client.post("/v1/summarize", json={"text": "x", "async": True})
    await wait_for_terminal(client, second.json()["job_id"])
    await wait_for_terminal(client, first.json()["job_id"])

    jobs = (await client.get("/v1/jobs")).json()["jobs"]
    assert len(jobs) == 2
    assert jobs[0]["kind"] == "summarize"


async def test_listing_filters_by_status(client, fake_summarizer):
    ok = await client.post("/v1/summarize", json={"text": "x", "async": True})
    await wait_for_terminal(client, ok.json()["job_id"])

    fake_summarizer.error = UpstreamError("nope")
    bad = await client.post("/v1/summarize", json={"text": "y", "async": True})
    await wait_for_terminal(client, bad.json()["job_id"])

    failed = (await client.get("/v1/jobs", params={"status": "failed"})).json()["jobs"]
    assert [j["id"] for j in failed] == [bad.json()["job_id"]]


async def test_listing_honours_the_limit(client):
    for _ in range(3):
        accepted = await client.post("/v1/summarize", json={"text": "x", "async": True})
        await wait_for_terminal(client, accepted.json()["job_id"])

    assert len((await client.get("/v1/jobs", params={"limit": 2})).json()["jobs"]) == 2


async def test_listing_rejects_a_bad_status(client):
    assert (await client.get("/v1/jobs", params={"status": "nonsense"})).status_code == 422


async def test_listing_rejects_an_out_of_range_limit(client):
    assert (await client.get("/v1/jobs", params={"limit": 0})).status_code == 422


async def test_empty_listing(client):
    assert (await client.get("/v1/jobs")).json()["jobs"] == []
