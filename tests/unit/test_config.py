import pytest
from pydantic import ValidationError

from app.config import _split_csv, get_settings
from tests.conftest import make_settings


def test_defaults_do_not_require_any_env(monkeypatch):
    monkeypatch.delenv("WSA_DEFAULT_MODEL", raising=False)
    settings = make_settings()
    assert settings.default_model == "anthropic:claude-opus-5"
    assert settings.respect_robots is True
    assert settings.api_keys == ()
    assert settings.request_timeout_seconds > 0


def test_env_overrides_are_picked_up(monkeypatch):
    monkeypatch.setenv("WSA_DEFAULT_MODEL", "ollama:llama3.1:8b")
    monkeypatch.setenv("WSA_RESPECT_ROBOTS", "false")
    settings = make_settings()
    assert settings.default_model == "ollama:llama3.1:8b"
    assert settings.respect_robots is False


def test_api_keys_parse_from_comma_separated_string(monkeypatch):
    monkeypatch.setenv("WSA_API_KEYS", "alpha, beta ,")
    settings = make_settings()
    assert settings.api_keys == ("alpha", "beta")


def test_api_keys_accept_a_real_sequence():
    settings = make_settings(api_keys=("one", "two"))
    assert settings.api_keys == ("one", "two")


def test_auth_is_disabled_when_no_keys_configured():
    assert make_settings().auth_enabled is False
    assert make_settings(api_keys=("k",)).auth_enabled is True


def test_negative_timeout_is_rejected():
    with pytest.raises(ValidationError):
        make_settings(request_timeout_seconds=-1)


def test_get_settings_is_cached():
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()


def test_api_keys_pass_through_an_existing_tuple():
    assert make_settings(api_keys=("already", "a", "tuple")).api_keys == (
        "already",
        "a",
        "tuple",
    )


def test_split_csv_normalises_each_supported_input_shape():
    assert _split_csv("a, b ,,c") == ("a", "b", "c")
    assert _split_csv(["a", "b"]) == ("a", "b")
    assert _split_csv(("a",)) == ("a",)
    assert _split_csv(None) is None
