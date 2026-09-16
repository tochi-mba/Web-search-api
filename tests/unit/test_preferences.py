"""One person's choices, and what this service does with them -- and without them.

Every test that reads settings-api here uses its shared fake, including with it
switched off, because the outage is the case most services forget and the one
their users notice.
"""

from __future__ import annotations

from typing import Any

import pytest
from settings_client import Fallback, OnUnavailable
from settings_client.testing import FakeSettingsClient

from app.core.errors import PreferencesUnavailableError
from app.services.preferences import (
    PROFILE_UNKNOWN,
    PROVIDERS_UNKNOWN,
    REFUSED,
    DeploymentPreferences,
    Preferences,
    SettingsApiPreferences,
    build_preference_source,
    deployment_preferences,
)
from tests.conftest import make_settings

USER_TOKEN = "a-user-token-from-keyring"
SETTINGS_API_TOKEN = "settings-api-token-for-web-search-0123"
HOUR = 3_600

FALLBACKS = {
    "default_model": Fallback(default=None, on_unavailable=OnUnavailable.USE_DEFAULT),
    "search_backend": Fallback(default="google", on_unavailable=OnUnavailable.USE_DEFAULT),
    "max_content_chars": Fallback(default=40_000, on_unavailable=OnUnavailable.USE_DEFAULT),
    "job_retention_hours": Fallback(default=1, on_unavailable=OnUnavailable.USE_DEFAULT),
    "disabled_providers": Fallback(default=[], on_unavailable=OnUnavailable.REFUSE),
    "default_profile": Fallback(default="personal", on_unavailable=OnUnavailable.REFUSE),
}


def reading(client: FakeSettingsClient, **overrides: Any) -> SettingsApiPreferences:
    return SettingsApiPreferences(client=client, settings=make_settings(**overrides))


class TestWithoutSettingsApi:
    def test_the_configuration_is_what_everybody_gets(self) -> None:
        settings = make_settings(
            default_model="ollama:llama3",
            max_content_chars=10_000,
            search_backend="searxng",
            disabled_providers=("groq",),
            job_retention_seconds=600,
            keyring_default_profile="work",
        )

        assert deployment_preferences(settings) == Preferences(
            default_model="ollama:llama3",
            max_content_chars=10_000,
            search_backend="searxng",
            disabled_providers=frozenset({"groq"}),
            job_retention_seconds=600,
            default_profile="work",
        )

    async def test_nobody_is_asked_when_settings_api_is_not_configured(self) -> None:
        settings = make_settings()
        source = build_preference_source(settings)

        assert isinstance(source, DeploymentPreferences)
        assert await source.for_token(USER_TOKEN) == deployment_preferences(settings)
        await source.aclose()

    async def test_a_configured_settings_api_is_asked_per_person(self) -> None:
        source = build_preference_source(
            make_settings(
                settings_api_base_url="https://settings.test",
                settings_api_token=SETTINGS_API_TOKEN,
            )
        )

        assert isinstance(source, SettingsApiPreferences)
        await source.aclose()

    async def test_a_substituted_client_is_the_one_asked(self) -> None:
        client = FakeSettingsClient()

        await build_preference_source(make_settings(), client=client).for_token(USER_TOKEN)

        assert client.resolves == 1


class TestAPersonsChoices:
    async def test_they_become_the_limits_a_request_runs_under(self) -> None:
        client = FakeSettingsClient()
        client.seed(
            "search",
            {
                "default_model": "ollama:llama3.1:8b",
                "max_content_chars": 8_000,
                "search_backend": "searxng",
                "disabled_providers": ["openai"],
                "job_retention_hours": 24,
                "default_profile": "work",
            },
        )

        preferences = await reading(client, job_retention_seconds=600).for_token(USER_TOKEN)

        assert preferences == Preferences(
            default_model="ollama:llama3.1:8b",
            max_content_chars=8_000,
            search_backend="searxng",
            disabled_providers=frozenset({"openai"}),
            job_retention_seconds=24 * HOUR,
            default_profile="work",
        )

    async def test_a_ceiling_can_be_narrowed_and_never_raised(self) -> None:
        client = FakeSettingsClient()
        client.seed("search", {"max_content_chars": 80_000})

        preferences = await reading(client, max_content_chars=10_000).for_token(USER_TOKEN)

        assert preferences.max_content_chars == 10_000

    async def test_retention_is_theirs_to_lengthen(self) -> None:
        client = FakeSettingsClient()
        client.seed("search", {"job_retention_hours": 168})

        preferences = await reading(client, job_retention_seconds=HOUR).for_token(USER_TOKEN)

        assert preferences.job_retention_seconds == 168 * HOUR

    async def test_deployment_disabled_providers_cannot_be_re_enabled(self) -> None:
        client = FakeSettingsClient()
        client.seed("search", {"disabled_providers": ["openai"]})

        preferences = await reading(client, disabled_providers=("groq",)).for_token(USER_TOKEN)

        assert preferences.disabled_providers == frozenset({"openai", "groq"})

    async def test_a_request_with_no_caller_asks_nobody(self) -> None:
        client = FakeSettingsClient()
        settings = make_settings()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(None)

        assert preferences == deployment_preferences(settings)
        assert client.resolves == 0


class TestWhenSettingsApiCannotBeReached:
    async def test_never_having_answered_leaves_the_configuration_and_unknown_refuse_keys(
        self,
    ) -> None:
        client = FakeSettingsClient()
        client.unavailable = True
        settings = make_settings()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(
            USER_TOKEN
        )

        assert preferences.default_model == settings.default_model
        assert preferences.max_content_chars == settings.max_content_chars
        assert preferences.search_backend == settings.search_backend
        assert preferences.job_retention_seconds == settings.job_retention_seconds
        assert preferences.disabled_providers is None
        assert preferences.default_profile is None

    async def test_known_fallbacks_are_used_and_refuse_keys_are_still_not_guessed(self) -> None:
        client = FakeSettingsClient(fallbacks={"search": FALLBACKS})
        client.unavailable = True

        preferences = await reading(client, job_retention_seconds=600).for_token(USER_TOKEN)

        assert preferences.search_backend == "google"
        assert preferences.max_content_chars == 40_000
        assert preferences.job_retention_seconds == HOUR
        assert preferences.disabled_providers is None
        assert preferences.default_profile is None

    async def test_reading_disabled_providers_fails_rather_than_guessing(self) -> None:
        client = FakeSettingsClient(fallbacks={"search": FALLBACKS})
        client.unavailable = True
        preferences = await reading(client).for_token(USER_TOKEN)

        with pytest.raises(PreferencesUnavailableError, match=PROVIDERS_UNKNOWN):
            preferences.require_disabled_providers()


class TestWhenSettingsApiRefusesThisService:
    async def test_the_refusal_is_not_hidden_behind_defaults(self) -> None:
        client = FakeSettingsClient()
        client.rejects["search"] = (403, "web-search-api was not granted search")

        with pytest.raises(PreferencesUnavailableError) as caught:
            await reading(client).for_token(USER_TOKEN)

        assert str(caught.value) == REFUSED
        assert "granted" not in str(caught.value)


class TestValuesThatCannotBeUsed:
    @pytest.mark.parametrize("value", [True, "8000", 0, -1, None])
    async def test_an_unusable_ceiling_leaves_the_configuration(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("search", {"max_content_chars": value})

        preferences = await reading(client, max_content_chars=9_000).for_token(USER_TOKEN)

        assert preferences.max_content_chars == 9_000

    async def test_a_missing_setting_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.seed("search", {})
        settings = make_settings()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(
            USER_TOKEN
        )

        assert preferences.default_model == settings.default_model
        assert preferences.max_content_chars == settings.max_content_chars
        assert preferences.search_backend == settings.search_backend
        assert preferences.disabled_providers == frozenset()
        assert preferences.job_retention_seconds == settings.job_retention_seconds
        assert preferences.default_profile == settings.keyring_default_profile

    async def test_the_key_is_logged_and_the_value_never_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logged: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def capture(*args: object, **kwargs: object) -> None:
            logged.append((args, kwargs))

        monkeypatch.setattr("app.services.preferences.logger.warning", capture)
        client = FakeSettingsClient()
        client.seed("search", {"max_content_chars": "a-value-nobody-should-read"})

        await reading(client).for_token(USER_TOKEN)

        assert any(kwargs.get("key") == "max_content_chars" for _args, kwargs in logged)
        assert all("a-value-nobody-should-read" not in f"{args}{kwargs}" for args, kwargs in logged)

    async def test_a_profile_that_is_not_a_name_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.seed("search", {"default_profile": 7})

        preferences = await reading(client, keyring_default_profile="default").for_token(USER_TOKEN)

        assert preferences.default_profile == "default"

    async def test_an_unusable_search_backend_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.seed("search", {"search_backend": 3})

        preferences = await reading(client, search_backend="searxng").for_token(USER_TOKEN)

        assert preferences.search_backend == "searxng"

    async def test_an_unusable_disabled_list_leaves_the_deployment_filter(self) -> None:
        client = FakeSettingsClient()
        client.seed("search", {"disabled_providers": "openai"})

        preferences = await reading(client, disabled_providers=("groq",)).for_token(USER_TOKEN)

        assert preferences.disabled_providers == frozenset({"groq"})

    async def test_a_null_default_model_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.seed("search", {"default_model": None})

        preferences = await reading(client, default_model="ollama:llama3").for_token(USER_TOKEN)

        assert preferences.default_model == "ollama:llama3"


class TestChoosingAProfile:
    def test_a_named_profile_is_used_as_named(self) -> None:
        preferences = deployment_preferences(make_settings())

        assert preferences.profile("work") == "work"

    def test_the_default_fills_in_when_none_is_named(self) -> None:
        preferences = deployment_preferences(make_settings(keyring_default_profile="personal"))

        assert preferences.profile(None) == "personal"

    def test_an_unknown_default_refuses_rather_than_guessing(self) -> None:
        preferences = Preferences(
            default_model="a:b",
            max_content_chars=1,
            search_backend="google",
            disabled_providers=frozenset(),
            job_retention_seconds=1,
            default_profile=None,
        )

        with pytest.raises(PreferencesUnavailableError) as caught:
            preferences.profile(None)

        assert str(caught.value) == PROFILE_UNKNOWN

    def test_a_named_profile_needs_no_default(self) -> None:
        preferences = Preferences(
            default_model="a:b",
            max_content_chars=1,
            search_backend="google",
            disabled_providers=frozenset(),
            job_retention_seconds=1,
            default_profile=None,
        )

        assert preferences.profile("work") == "work"
