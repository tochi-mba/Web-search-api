"""Shared fixtures and helpers."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings


def make_settings(**overrides: Any) -> Settings:
    """Build Settings that ignore any developer .env file.

    ``_env_file`` is a pydantic-settings runtime keyword that is not part of the
    generated ``__init__`` signature, so the cast is confined to this one place
    rather than repeated across the suite.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings for tests."""
    return make_settings(
        json_logs=True,
        log_level="INFO",
        respect_robots=True,
        request_timeout_seconds=5.0,
    )
