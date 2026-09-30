#!/usr/bin/env python
"""Manual smoke test against a running server.

Deliberately outside the test suite: it costs money and depends on third
parties. Run it by hand after a meaningful change.

    make run                       # in one shell
    uv run python scripts/smoke.py # in another

It exits 0 only when every call it made succeeded: health, readiness, a non-empty model
catalogue, and one summarise, scrape and search, with no batch item in error.

The service reads no provider key from anywhere; keys live in keyring and are resolved
for whoever the user token says is asking. Without ``--token`` (or ``SMOKE_USER_TOKEN``)
only a credential-free runtime such as a local Ollama can answer. ``--api-key`` is for a
deployment with the ``X-API-Key`` gate on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

import httpx

DEFAULT_BASE = "http://localhost:8006"

#: Not ``WSA_``-prefixed: the service refuses unknown ``WSA_`` variables, and this one may
#: sit in the same shell or ``.env`` as the service's own.
IDENTITY_VARIABLE = "SMOKE_USER_TOKEN"

NO_MODELS = (
    "No models available. Without a user token only credential-free runtimes (a local "
    "Ollama, LM Studio, ...) can appear; the service never reads a provider key from the "
    "environment. Start one, or connect a provider in keyring and pass --token."
)


def show(title: str, payload: object) -> None:
    """Print one step's answer, trimmed to stay readable."""
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2)[:2000])


class Run:
    """The calls a smoke run makes, and every way they fell short."""

    def __init__(self, client: httpx.Client) -> None:
        """Wrap a client already carrying any credentials."""
        self.client = client
        self.failures: list[str] = []

    def call(self, title: str, method: str, path: str, **kwargs: Any) -> Any:
        """Make one call and show it; a non-200 answer is recorded as a failure."""
        response = self.client.request(method, path, **kwargs)
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        show(f"{title} ({response.status_code})", body)
        if response.status_code != httpx.codes.OK:
            self.failures.append(f"{title}: HTTP {response.status_code}")
            return None
        return body

    def batch(self, title: str, path: str, payload: dict[str, Any]) -> None:
        """Make a batch call, which answers 200 even when every item in it failed."""
        body = self.call(title, "POST", path, json=payload)
        if body is None:
            return
        failed = [item for item in body["results"] if item["status"] != "ok"]
        if failed:
            codes = ", ".join(str((item.get("error") or {}).get("code")) for item in failed)
            self.failures.append(f"{title}: {len(failed)} item(s) in error ({codes})")


def smoke(run: Run, args: argparse.Namespace) -> None:
    """Exercise the service end to end, recording what failed rather than stopping."""
    run.call("health", "GET", "/health")
    run.call("readiness", "GET", "/health/ready")

    catalog = run.call("models", "GET", "/v1/models")
    if catalog is None:
        return
    if not catalog["models"]:
        run.failures.append(f"models: {NO_MODELS}")
        return

    model = args.model or catalog["default_model"]
    print(f"\nUsing model: {model}")

    summary = run.call(
        "summarize",
        "POST",
        "/v1/summarize",
        json={
            "text": (
                "Widget latency rose across three release cycles. The cause was "
                "queue contention in the dispatch layer rather than network "
                "transit. Teams that sharded the dispatch queue saw latency fall "
                "by roughly forty percent."
            ),
            "model": model,
            "additional_notes": "Focus on the remediation.",
        },
    )
    if summary is not None and not summary.get("summary"):
        run.failures.append("summarize: the answer carried no summary")

    run.batch("scrape", "/v1/scrape", {"urls": [args.url], "model": model})
    if not args.skip_search:
        run.batch("search", "/v1/search", {"queries": [{"query": args.query}], "model": model})


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    """Run the smoke test and return the process exit code."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--model", default=None, help="Namespaced model id.")
    parser.add_argument("--query", default="fastapi testing best practices")
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument(
        "--token",
        default=os.environ.get(IDENTITY_VARIABLE),
        help=f"A keyring user token, sent as a bearer token. Defaults to ${IDENTITY_VARIABLE}.",
    )
    parser.add_argument("--api-key", default=None, help="Sent as X-API-Key.")
    args = parser.parse_args(argv)

    headers = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    if args.api_key:
        headers["X-API-Key"] = args.api_key

    with httpx.Client(
        base_url=args.base_url, timeout=180.0, headers=headers, transport=transport
    ) as client:
        run = Run(client)
        smoke(run, args)

    if run.failures:
        print("\nSmoke test FAILED:")
        for failure in run.failures:
            print(f"  - {failure}")
        return 1
    print("\nSmoke test passed: every call succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
