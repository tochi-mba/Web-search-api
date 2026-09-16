import pytest
from pydantic import ValidationError

from app.config import UnknownSettingError, _split_csv, check_for_unknown_env_vars, get_settings
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


# --- unknown, removed and malformed settings -------------------------------- #

SERVICE_TOKEN = "svc-token-0123456789abcdef0123456789"


def test_an_invented_setting_is_refused():
    with pytest.raises(ValidationError, match="Extra inputs"):
        make_settings(enable_everything=True)


def test_a_misspelled_variable_is_a_startup_error_not_a_silent_default():
    with pytest.raises(UnknownSettingError, match="WSA_DEFALT_MODEL"):
        check_for_unknown_env_vars({"WSA_DEFALT_MODEL": "anthropic:claude-opus-5"})


@pytest.mark.parametrize("name", ["WSA_ENABLED_PROVIDERS", "WSA_MAX_CONCURRENCY_PER_HOST"])
def test_a_removed_setting_says_why_it_went(name):
    # Both were declared and read nowhere; a deployment setting one believed it did something.
    with pytest.raises(UnknownSettingError, match=rf"{name} \(removed:"):
        check_for_unknown_env_vars({name: "2"})


def test_recognised_and_unrelated_variables_pass():
    check_for_unknown_env_vars(
        {
            "WSA_DEFAULT_MODEL": "anthropic:claude-opus-5",
            # Read directly by the browser adapter rather than through settings.
            "WSA_BROWSER_EXECUTABLE_PATH": "/usr/bin/chromium",
            "OLLAMA_BASE_URL": "http://gpu-box:11434",
        }
    )


def test_get_settings_runs_the_check(monkeypatch):
    monkeypatch.setenv("WSA_MAX_CONCURRENCY_PER_HOST", "2")
    get_settings.cache_clear()
    try:
        with pytest.raises(UnknownSettingError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_the_keyring_issuer_and_refetch_floor_have_the_family_defaults():
    settings = make_settings()
    assert settings.keyring_issuer == "http://127.0.0.1:8001"
    assert settings.jwks_min_refetch_seconds == 60.0


def test_an_empty_service_token_means_keyring_is_off():
    assert make_settings(keyring_base_url="http://127.0.0.1:8001").keyring_enabled is False


def test_a_short_service_token_is_refused_without_being_echoed():
    with pytest.raises(ValidationError) as caught:
        make_settings(keyring_service_token="too-short-token")
    assert "too-short-token" not in str(caught.value)


def test_the_service_token_never_appears_in_a_dump():
    settings = make_settings(keyring_base_url="http://k.test", keyring_service_token=SERVICE_TOKEN)
    assert settings.keyring_enabled is True
    assert SERVICE_TOKEN not in f"{settings!r} {settings.model_dump()} {settings.model_dump_json()}"


@pytest.mark.parametrize("name", ["", " web-search-api", "web-search-api.jobs"])
def test_an_audience_keyring_could_never_mint_is_refused(name):
    with pytest.raises(ValidationError):
        make_settings(keyring_service_name=name)


def test_the_example_file_is_valid_configuration():
    from pathlib import Path

    from app.config import Settings

    example = Path(__file__).parents[2] / ".env.example"
    settings = Settings(_env_file=example)  # type: ignore[call-arg]
    assert settings.keyring_issuer == "http://127.0.0.1:8001"
    assert settings.provider_base_urls["ollama"] == "http://localhost:11434"
    assert settings.settings_api is None


SETTINGS_API_TOKEN = "settings-api-token-for-web-search-0123"


def test_settings_api_is_off_when_neither_is_set():
    assert make_settings().settings_api is None


def test_settings_api_is_on_when_both_are_set():
    settings = make_settings(
        settings_api_base_url="https://settings.test", settings_api_token=SETTINGS_API_TOKEN
    )
    assert settings.settings_api is not None
    base_url, token = settings.settings_api
    assert base_url == "https://settings.test"
    assert token.get_secret_value() == SETTINGS_API_TOKEN


@pytest.mark.parametrize(
    "overrides",
    [
        {"settings_api_base_url": "https://settings.test"},
        {"settings_api_token": SETTINGS_API_TOKEN},
    ],
)
def test_half_a_settings_api_configuration_is_refused(overrides):
    with pytest.raises(ValidationError, match="must be set together"):
        make_settings(**overrides)


def test_a_blank_settings_api_url_means_off():
    assert make_settings(settings_api_base_url="").settings_api_base_url is None


def test_an_empty_settings_api_token_object_means_off():
    from pydantic import SecretStr

    settings = make_settings(settings_api_token=SecretStr(""))
    assert settings.settings_api_token is None


def test_a_short_settings_api_token_is_refused_without_being_echoed():
    with pytest.raises(ValidationError) as caught:
        make_settings(
            settings_api_base_url="https://settings.test", settings_api_token="short-token"
        )
    assert "short-token" not in str(caught.value)


def test_the_settings_api_token_never_appears_in_a_dump():
    settings = make_settings(
        settings_api_base_url="https://settings.test", settings_api_token=SETTINGS_API_TOKEN
    )
    assert (
        SETTINGS_API_TOKEN
        not in f"{settings!r} {settings.model_dump()} {settings.model_dump_json()}"
    )
