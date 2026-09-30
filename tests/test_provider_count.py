"""Everywhere that states how many LLM providers there are states the real number.

The count is derived from what bootstrap builds, so adding a row to the spec table changes
it; this fails until the prose that repeats it is updated too.
"""

import tomllib
from pathlib import Path

import httpx
import pytest

from app.bootstrap import NATIVE_PROVIDERS, build_llm_providers, llm_provider_count
from app.main import create_app
from tests.conftest import make_settings

ROOT = Path(__file__).resolve().parent.parent


async def test_the_count_is_every_provider_bootstrap_builds():
    async with httpx.AsyncClient() as client:
        providers = build_llm_providers(make_settings(), client)
    assert len(providers) == llm_provider_count()
    assert [p.name for p in providers[: len(NATIVE_PROVIDERS)]] == list(NATIVE_PROVIDERS)


def pyproject_description() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    description: str = tomllib.loads(text)["project"]["description"]
    return description


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(pyproject_description(), id="pyproject.toml"),
        pytest.param(create_app(make_settings()).summary, id="OpenAPI summary"),
        pytest.param((ROOT / "README.md").read_text(encoding="utf-8"), id="README.md"),
        pytest.param((ROOT / "AGENTS.md").read_text(encoding="utf-8"), id="AGENTS.md"),
    ],
)
def test_every_description_states_the_real_count(text):
    assert f"any of {llm_provider_count()} LLM providers" in " ".join(text.split())
