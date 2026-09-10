"""Application settings, loaded from the environment with a ``WSA_`` prefix."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any

from pydantic import BeforeValidator, Field
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
        extra="ignore",
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

    #: Extra provider keys to force-enable even without an API key in the env.
    enabled_providers: CsvTuple = ()
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
    max_concurrency_per_host: int = Field(default=2, gt=0)

    # -- Keyring ----------------------------------------------------------- #
    #: Where the credential vault lives. Empty disables keyring, leaving only
    #: providers that need no credential usable.
    keyring_base_url: str = ""
    keyring_service_token: str = ""
    keyring_service_name: str = "web-search-api"
    """This service's name. Must match the audience callers mint tokens for."""

    keyring_default_profile: str = "personal"
    keyring_timeout_seconds: float = Field(default=10.0, gt=0)
    jwks_cache_seconds: float = Field(default=3600.0, ge=0)

    @property
    def keyring_enabled(self) -> bool:
        """Whether credentials can be resolved at all."""
        return bool(self.keyring_base_url and self.keyring_service_token)

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
    return Settings()
