"""scripts/smoke.py reports success only for what it has checked.

It runs by hand against a live server, so these tests stand a fake server in front of it
and hold it to its exit code: zero only when every call it makes succeeded.
"""

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke.py"

MODEL = "ollama:llama3.1:8b"
SUMMARY = {"summary": {"text": "fine"}}
OK_ITEM = {"status": "ok"}
ERROR_ITEM = {"status": "error", "error": {"code": "upstream_error"}}


def load_smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def smoke() -> ModuleType:
    return load_smoke()


def server(**overrides: tuple[int, Any]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A fake service answering every route the script calls, all healthy by default."""
    answers: dict[str, tuple[int, Any]] = {
        "/health": (200, {"status": "ok"}),
        "/health/ready": (200, {"ready": True, "components": []}),
        "/v1/models": (
            200,
            {
                "default_model": MODEL,
                "models": [{"id": MODEL}],
                "providers": [{"name": "ollama", "status": "available", "detail": ""}],
            },
        ),
        "/v1/summarize": (200, SUMMARY),
        "/v1/scrape": (200, {"results": [OK_ITEM]}),
        "/v1/search": (200, {"results": [OK_ITEM]}),
    }
    answers.update({f"/{key.replace('_', '/')}": value for key, value in overrides.items()})
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = answers[request.url.path]
        return httpx.Response(status, content=json.dumps(body).encode())

    return httpx.MockTransport(handle), seen


def run(smoke: ModuleType, transport: httpx.MockTransport, *argv: str) -> int:
    main: Callable[..., int] = smoke.main
    return main(list(argv), transport=transport)


def test_a_healthy_service_passes(smoke):
    transport, _ = server()
    assert run(smoke, transport) == 0


def test_a_failed_scrape_item_fails_the_run(smoke, capsys):
    transport, _ = server(v1_scrape=(200, {"results": [ERROR_ITEM]}))
    assert run(smoke, transport) == 1
    assert "scrape" in capsys.readouterr().out.split("FAILED", 1)[1]


def test_a_failed_search_query_fails_the_run(smoke):
    transport, _ = server(v1_search=(200, {"results": [OK_ITEM, ERROR_ITEM]}))
    assert run(smoke, transport) == 1


def test_a_problem_response_fails_the_run(smoke):
    transport, _ = server(v1_summarize=(502, {"code": "upstream_error"}))
    assert run(smoke, transport) == 1


def test_a_degraded_readiness_fails_the_run(smoke):
    transport, _ = server(health_ready=(503, {"ready": False, "components": []}))
    assert run(smoke, transport) == 1


def test_skipping_search_does_not_call_it(smoke):
    transport, seen = server(v1_search=(500, {}))
    assert run(smoke, transport, "--skip-search") == 0
    assert "/v1/search" not in {request.url.path for request in seen}


def test_no_models_fails_and_does_not_blame_a_missing_environment_key(smoke, capsys):
    transport, _ = server(v1_models=(200, {"default_model": MODEL, "models": [], "providers": []}))
    assert run(smoke, transport) == 1
    out = capsys.readouterr().out
    assert "--token" in out
    assert "set a provider key" not in out


def test_the_token_and_api_key_are_sent_and_never_printed(smoke, capsys):
    transport, seen = server()
    assert run(smoke, transport, "--token", "user-secret", "--api-key", "gate-secret") == 0
    assert all(r.headers["authorization"] == "Bearer user-secret" for r in seen)
    assert all(r.headers["x-api-key"] == "gate-secret" for r in seen)
    out = capsys.readouterr().out
    assert "user-secret" not in out
    assert "gate-secret" not in out


def test_the_token_can_come_from_the_environment(smoke, monkeypatch):
    monkeypatch.setenv("SMOKE_USER_TOKEN", "from-env")
    transport, seen = server()
    assert run(smoke, transport) == 0
    assert seen[0].headers["authorization"] == "Bearer from-env"
