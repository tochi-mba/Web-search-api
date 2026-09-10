#!/usr/bin/env python
"""Store provider keys in keyring with the header configuration each vendor needs.

Keyring's api-key connections default to ``Authorization: Bearer {value}``.
Anthropic wants ``x-api-key``, and some vendors want the key in the query
string. Store a key with the wrong shape and the provider returns 401 with no
hint as to why.

This service already knows the right answer for every provider it supports, so
this script reads that table and writes correctly configured connections.

    # what would be stored, without storing anything
    uv run python scripts/provision_keyring.py --print

    # store the keys currently in your environment
    export KEYRING_SESSION_TOKEN=...            # from POST /v1/auth/login
    export ANTHROPIC_API_KEY=... GROQ_API_KEY=...
    uv run python scripts/provision_keyring.py --profile personal

    # check what is already connected and whether it is configured correctly
    uv run python scripts/provision_keyring.py --check
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

import httpx

from app.services.llm.providers.anthropic import PROVIDER_KEY as ANTHROPIC_KEY
from app.services.llm.specs import OPENAI_COMPATIBLE_SPECS, AuthStyle, ProviderSpec

#: Where each provider's key is conventionally found in the environment. Used
#: only to migrate existing keys into keyring; nothing reads these at runtime.
ENV_HINTS: dict[str, str] = {
    ANTHROPIC_KEY: "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "cohere": "COHERE_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "zai": "ZAI_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "serper": "SERPER_API_KEY",
}


@dataclass(frozen=True)
class Connection:
    """One credential to store, shaped the way its vendor expects."""

    service: str
    header: str
    template: str
    in_query: bool
    query_name: str
    env_var: str

    @property
    def body_without_key(self) -> dict[str, object]:
        """The connection configuration, minus the secret itself."""
        body: dict[str, object] = {"header": self.header, "template": self.template}
        if self.in_query:
            body |= {"in_query": True, "query_name": self.query_name}
        return body


def anthropic_connection() -> Connection:
    """Anthropic is a native adapter, so it is not in the compatible table."""
    return Connection(
        service=ANTHROPIC_KEY,
        header="x-api-key",
        template="{value}",
        in_query=False,
        query_name="",
        env_var=ENV_HINTS[ANTHROPIC_KEY],
    )


def connection_for(spec: ProviderSpec) -> Connection:
    """Derive a keyring connection from a provider's spec row."""
    return Connection(
        service=spec.key,
        header=spec.default_header,
        template=spec.default_template,
        in_query=spec.auth is AuthStyle.QUERY,
        query_name="key",
        env_var=ENV_HINTS.get(spec.key, f"{spec.key.upper()}_API_KEY"),
    )


def serper_connection() -> Connection:
    """The search backend key, stored like any other credential."""
    return Connection(
        service="serper",
        header="X-API-KEY",
        template="{value}",
        in_query=False,
        query_name="",
        env_var=ENV_HINTS["serper"],
    )


def all_connections() -> list[Connection]:
    """Every credential this service can use, in a stable order."""
    connections = [anthropic_connection()]
    connections += [connection_for(spec) for spec in OPENAI_COMPATIBLE_SPECS if spec.requires_key]
    connections.append(serper_connection())
    return sorted(connections, key=lambda c: c.service)


def put_url(base_url: str, profile: str, service: str) -> str:
    """Where a connection is written."""
    return f"{base_url.rstrip('/')}/v1/profiles/{profile}/connections/{service}/api-key"


def print_plan(base_url: str, profile: str, connections: list[Connection]) -> None:
    """Emit the curl commands rather than running anything."""
    for conn in connections:
        body = {"api_key": f"${conn.env_var}", **conn.body_without_key}
        fields = ", ".join(f'"{k}": {_quote(v)}' for k, v in body.items())
        print(f"# {conn.service}  (key from ${conn.env_var})")
        print(
            f"curl -sX PUT {put_url(base_url, profile, conn.service)} \\\n"
            f'  -H "Authorization: Bearer $KEYRING_SESSION_TOKEN" \\\n'
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -d '{{{fields}}}'\n"
        )


def _quote(value: object) -> str:
    """Render a JSON value for the printed curl body."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return f'"{value}"'


def provision(
    client: httpx.Client,
    *,
    base_url: str,
    profile: str,
    session_token: str,
    connections: list[Connection],
) -> int:
    """Store every credential found in the environment. Returns an exit code."""
    stored, skipped, failed = 0, 0, 0

    for conn in connections:
        key = os.environ.get(conn.env_var, "").strip()
        if not key:
            skipped += 1
            continue

        response = client.put(
            put_url(base_url, profile, conn.service),
            headers={"Authorization": f"Bearer {session_token}"},
            json={"api_key": key, **conn.body_without_key},
        )
        if response.status_code >= 400:
            print(f"  FAILED  {conn.service}: {response.status_code} {response.text[:120]}")
            failed += 1
        else:
            print(f"  stored  {conn.service}  ({conn.header})")
            stored += 1

    print(f"\n{stored} stored, {skipped} skipped (no key in the environment), {failed} failed.")
    return 1 if failed else 0


def check(client: httpx.Client, *, base_url: str, profile: str, session_token: str) -> int:
    """Report which services are connected for this profile."""
    response = client.get(
        f"{base_url.rstrip('/')}/v1/profiles/{profile}",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    if response.status_code >= 400:
        print(f"Could not read profile '{profile}': {response.status_code} {response.text[:200]}")
        return 1

    payload = response.json()
    connections = payload.get("connections", [])
    if not connections:
        print(f"Profile '{profile}' has no connections yet.")
        return 0

    known = {c.service for c in all_connections()}
    print(f"Profile '{profile}':")
    for entry in connections:
        service = entry.get("service", "?")
        marker = "" if service in known else "   (not a provider this service uses)"
        live = entry.get("live", entry.get("status", ""))
        print(f"  {service:<20} {live}{marker}")
    return 0


def main() -> int:
    """Run the requested action."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KEYRING_BASE_URL", "http://127.0.0.1:8001"),
    )
    parser.add_argument("--profile", default="personal")
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print the curl commands instead of running them.",
    )
    parser.add_argument("--check", action="store_true", help="Report what is already connected.")
    parser.add_argument(
        "--only", nargs="*", help="Limit to these services, e.g. --only anthropic groq."
    )
    args = parser.parse_args()

    connections = all_connections()
    if args.only:
        wanted = set(args.only)
        connections = [c for c in connections if c.service in wanted]
        missing = wanted - {c.service for c in connections}
        if missing:
            print(f"Unknown services: {', '.join(sorted(missing))}")
            return 2

    if args.print_only:
        print_plan(args.base_url, args.profile, connections)
        return 0

    session_token = os.environ.get("KEYRING_SESSION_TOKEN", "").strip()
    if not session_token:
        print(
            "Set KEYRING_SESSION_TOKEN to a keyring session token "
            "(POST /v1/auth/login), or use --print to see the commands."
        )
        return 2

    with httpx.Client(timeout=30.0) as client:
        if args.check:
            return check(
                client,
                base_url=args.base_url,
                profile=args.profile,
                session_token=session_token,
            )
        return provision(
            client,
            base_url=args.base_url,
            profile=args.profile,
            session_token=session_token,
            connections=connections,
        )


if __name__ == "__main__":
    sys.exit(main())
