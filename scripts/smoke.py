#!/usr/bin/env python
"""Manual smoke test against a running server with real credentials.

Deliberately outside the test suite: it costs money and depends on third
parties. Run it by hand after a meaningful change.

    make run                       # in one shell
    uv run python scripts/smoke.py # in another
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

DEFAULT_BASE = "http://localhost:8006"


def show(title: str, payload: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2)[:2000])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--model", default=None, help="Namespaced model id.")
    parser.add_argument("--query", default="fastapi testing best practices")
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--skip-search", action="store_true")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=180.0) as client:
        show("health", client.get("/health").json())
        show("readiness", client.get("/health/ready").json())

        catalog = client.get("/v1/models").json()
        show(
            "providers",
            {
                "default_model": catalog["default_model"],
                "available": [
                    p["name"] for p in catalog["providers"] if p["status"] == "available"
                ],
                "problems": [
                    {"name": p["name"], "status": p["status"], "detail": p["detail"]}
                    for p in catalog["providers"]
                    if p["status"] in ("unauthorized", "unreachable")
                ],
                "model_count": len(catalog["models"]),
            },
        )

        if not catalog["models"]:
            print("\nNo models available - set a provider key and try again.")
            return 1

        model = args.model or catalog["default_model"]
        print(f"\nUsing model: {model}")

        show(
            "summarize",
            client.post(
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
            ).json(),
        )

        show(
            "scrape",
            client.post(
                "/v1/scrape",
                json={"urls": [args.url], "model": model},
            ).json(),
        )

        if not args.skip_search:
            show(
                "search",
                client.post(
                    "/v1/search",
                    json={"queries": [{"query": args.query}], "model": model},
                ).json(),
            )

    print("\nSmoke test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
