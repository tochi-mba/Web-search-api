"""Application settings, loaded from the environment with a ``WSA_`` prefix."""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from typing import Annotated, Any, Self

from keyring_client import ExactAudience, check_service_token
from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app import constants


def _split_csv(value: Any) -> Any:
    """Allow list-valued settings to be supplied as a comma-separated string.

    ``pydantic-settings`` would otherwise try to JSON-decode the environment
    value, which makes ``WSA_API_KEYS=a,b`` fail confusingly.
    """
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list):
        return tuple(value)
    return value


CsvTuple = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    """Runtime configuration for the whole service."""

    model_config = SettingsConfigDict(
        env_prefix="WSA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        env_nested_delimiter="__",
        hide_input_in_errors=True,
    )

    # -- Service ---------------------------------------------------------- #
    service_name: str = "web-search-api"
    log_level: str = "INFO"
    json_logs: bool = True

    #: Static API keys. Empty means the API is unauthenticated.
    api_keys: CsvTuple = ()

    # -- LLM -------------------------------------------------------------- #
    default_model: str = constants.DEFAULT_MODEL
    model_cache_ttl_seconds: float = Field(default=constants.MODEL_CACHE_TTL_SECONDS, ge=0)
    provider_probe_timeout_seconds: float = Field(default=5.0, gt=0)
    max_content_chars: int = Field(default=constants.MAX_CONTENT_CHARS, gt=0)

    #: Base-URL overrides keyed by provider key, e.g. ``{"ollama": "http://gpu:11434"}``.
    provider_base_urls: dict[str, str] = Field(default_factory=dict)

    disabled_providers: CsvTuple = ()

    # -- Fetching --------------------------------------------------------- #
    request_timeout_seconds: float = Field(default=20.0, gt=0)
    max_redirects: int = Field(default=5, ge=0)
    max_response_bytes: int = Field(default=5_000_000, gt=0)
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    respect_robots: bool = True
    allow_private_networks: bool = False

    # -- Concurrency ------------------------------------------------------ #
    max_concurrency: int = Field(default=8, gt=0)

    # -- Keyring ----------------------------------------------------------- #
    #: Where the credential vault lives. Empty disables keyring, leaving only
    #: providers that need no credential usable.
    keyring_base_url: str = ""
    keyring_service_token: SecretStr = SecretStr("")
    keyring_issuer: str = "http://127.0.0.1:8001"
    keyring_service_name: str = "web-search-api"
    """This service's name. Must match the audience callers mint tokens for."""

    keyring_default_profile: str = "personal"
    keyring_timeout_seconds: float = Field(default=10.0, gt=0)
    jwks_cache_seconds: float = Field(default=3600.0, ge=0)
    jwks_min_refetch_seconds: float = Field(default=60.0, gt=0)
    jwks_stale_grace_seconds: float = Field(default=86400.0, ge=0)

    # -- Per-person settings ---------------------------------------------- #
    settings_api_base_url: str | None = None
    """Where settings-api is. Unset, every person gets this configuration as it stands.

    Set, each request reads its owner's ``search`` settings: default model, content
    ceiling, preferred search backend, disabled providers, job retention, and which
    profile they mean when they name none. The ceilings in this configuration still
    apply on top of what anybody chooses.
    """

    settings_api_token: SecretStr | None = None
    """This service's entry in settings-api's ``SETTINGS_API_SERVICES``.

    At least 32 characters, the rule settings-api enforces on its side. Its grant
    there needs ``audience_prefix`` ``web-search-api``: settings-api is shown the
    same user token keyring is.
    """

    @field_validator("keyring_service_token")
    @classmethod
    def _check_service_token(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value():
            check_service_token(value.get_secret_value())
        return value

    @field_validator("settings_api_base_url")
    @classmethod
    def _blank_settings_url_is_unset(cls, value: str | None) -> str | None:
        """``WSA_SETTINGS_API_BASE_URL=`` in a ``.env`` means off, not an empty URL."""
        return value or None

    @field_validator("settings_api_token", mode="before")
    @classmethod
    def _blank_settings_token_is_unset(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value():
            return None
        return value

    @field_validator("settings_api_token")
    @classmethod
    def _check_settings_api_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            check_service_token(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def _check_settings_api_is_whole(self) -> Self:
        """Refuse half a settings-api configuration.

        A URL with no token would be refused on every call, and a token with no URL
        is a secret configured for nothing. Either is somebody's mistake, and startup
        is the cheapest place to hear about it.
        """
        if (self.settings_api_base_url is None) != (self.settings_api_token is None):
            raise ValueError("settings_api_base_url and settings_api_token must be set together")
        return self

    @property
    def settings_api(self) -> tuple[str, SecretStr] | None:
        """Where settings-api is and how to authenticate to it, or ``None`` when unused."""
        if self.settings_api_base_url is None or self.settings_api_token is None:
            return None
        return self.settings_api_base_url, self.settings_api_token

    @field_validator("keyring_service_name")
    @classmethod
    def _check_audience(cls, value: str) -> str:
        return ExactAudience(value).name

    @property
    def keyring_enabled(self) -> bool:
        """Whether credentials can be resolved at all."""
        return bool(self.keyring_base_url and self.keyring_service_token.get_secret_value())

    # -- Background jobs --------------------------------------------------- #
    max_background_jobs: int = Field(default=4, gt=0, description="Concurrent background jobs.")
    job_retention_seconds: float = Field(
        default=3600.0, ge=0, description="How long a finished job stays readable."
    )
    max_stored_jobs: int = Field(
        default=1000, gt=0, description="Hard cap before the oldest finished jobs are evicted."
    )

    # -- Search ----------------------------------------------------------- #
    search_backend: str = "google"
    searxng_base_url: str = ""
    browser_headless: bool = True
    browser_navigation_timeout_ms: int = Field(default=20_000, gt=0)

    @property
    def auth_enabled(self) -> bool:
        """Whether inbound requests must present an API key."""
        return bool(self.api_keys)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    check_for_unknown_env_vars()
    return Settings()


class UnknownSettingError(ValueError):
    """A deployment supplied an unknown or removed WSA setting."""


def check_for_unknown_env_vars(environ: Mapping[str, str] | None = None) -> None:
    """Reject typos without echoing values that may contain credentials."""
    environ = os.environ if environ is None else environ
    known = {f"WSA_{name.upper()}" for name in Settings.model_fields}
    known.add("WSA_BROWSER_EXECUTABLE_PATH")
    removed = {"WSA_ENABLED_PROVIDERS", "WSA_MAX_CONCURRENCY_PER_HOST"}
    unknown = []
    for name in environ:
        canonical = name.upper()
        if canonical in removed:
            unknown.append(f"{name} (removed: this setting was never used)")
        elif canonical.startswith("WSA_") and canonical.split("__", 1)[0] not in known:
            unknown.append(name)
    if unknown:
        raise UnknownSettingError("Unknown settings: " + ", ".join(sorted(unknown)))
