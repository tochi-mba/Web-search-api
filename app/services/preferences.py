"""What one person has chosen, and what this service does when it cannot ask.

The deployment's configuration says how web-search-api behaves for everybody.
settings-api holds what each person has chosen within that, and this module is
the one place the two meet. Nothing is read at startup, and with no settings-api
configured every person gets the configuration as it stands -- exactly what the
service did before it read anybody's settings at all.

Three rules shape it.

**A person may narrow a ceiling and never raise it.** ``max_content_chars`` is
clamped to the deployment's cap. Disabled providers are a union: a person can
turn more providers off, never re-enable one the operator has turned off.
Retention is theirs, within the bounds settings-api already enforces.

**An outage degrades per setting.** Keys that fall back use the configuration
(or settings-api's own defaults, once this client has seen them). The two that
refuse -- ``disabled_providers`` and ``default_profile`` -- stay unknown, and
fail only the operation that actually needs them. Guessing the empty provider
list would send a query to a provider this person refused. Guessing ``personal``
would quietly act on the wrong account.

**A refusal is not an outage.** settings-api answering 401 or 403 means this
service is misconfigured, and serving defaults would hide that behind behaviour
that happens to work. The request fails instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from settings_client import (
    HttpSettingsClient,
    SettingsRefused,
    SettingsRejected,
    SettingsUnavailable,
)

from app import constants
from app.core.errors import PreferencesUnavailableError
from app.core.logging import get_logger
from app.services.llm.prompts import SummaryLength
from app.services.search.base import SafeSearch

if TYPE_CHECKING:
    from settings_client import ResolvedSettings, SettingsClient

    from app.config import Settings

logger = get_logger(__name__)

NAMESPACE = "search"
SECONDS_PER_HOUR = 3_600

PROFILE_UNKNOWN = (
    "your default profile could not be read from settings-api; name a profile in the "
    "request, or try again shortly"
)
PROVIDERS_UNKNOWN = (
    "which model providers you have disabled could not be read from settings-api "
    "and must not be guessed"
)
REFUSED = "settings-api did not accept this service's request for your settings"


@dataclass(frozen=True, slots=True)
class Preferences:
    """One person's choices, as this service applies them to one request."""

    default_model: str
    """Which model answers when a request does not name one."""

    max_content_chars: int
    """How much scraped text may be handed to a model, inside the deployment cap."""

    search_backend: str
    """Which search backend to try first. Failover still runs."""

    disabled_providers: frozenset[str] | None
    """Providers this person's queries must never be sent to.

    ``None`` when settings-api could not be asked and the answer must not be guessed.
    Always includes the deployment's own disabled list when the person's list is known.
    """

    job_retention_seconds: float
    """How long the record of a finished job this person submits stays readable."""

    default_profile: str | None
    """The profile a login uses when the request names none.

    ``None`` when settings-api could not be asked and the answer must not be guessed.
    """

    safe_search: SafeSearch | None = None
    """How hard this person filters explicit results. ``None`` when nobody has chosen:
    no settings-api is configured, and the request alone decides, as it always did."""

    recency_days: int | None = None
    """How recent a result must be when the request does not say. ``None`` is no filter."""

    language: str | None = None
    """Results' language when the request does not say. ``None`` is English, as always."""

    region: str | None = None
    """Where results are ranked for when the request does not say. ``None`` is the US."""

    blocked_domains: tuple[str, ...] = ()
    """Sites whose results this person never sees. Each also covers its subdomains."""

    read_top_pages: int = 0
    """How many top result pages a summary is written from when the request does not say.
    ``0`` is titles and snippets only, as always."""

    summary_length: SummaryLength = SummaryLength.STANDARD
    """How long a summary is."""

    research_notes: str | None = None
    """Standing guidance for every summary, added after a request's own notes."""

    def profile(self, requested: str | None) -> str:
        """The profile a request is resolved with: the one it named, or the default.

        Raises:
            PreferencesUnavailableError: the request named none and the default is unknown.
        """
        if requested is not None:
            return requested
        if self.default_profile is None:
            raise PreferencesUnavailableError(PROFILE_UNKNOWN)
        return self.default_profile

    def require_disabled_providers(self) -> frozenset[str]:
        """Providers that must not see this person's queries.

        Raises:
            PreferencesUnavailableError: the list is unknown and must not be guessed.
        """
        if self.disabled_providers is None:
            raise PreferencesUnavailableError(PROVIDERS_UNKNOWN)
        return self.disabled_providers


@runtime_checkable
class PreferenceSource(Protocol):
    """Where a request's preferences come from."""

    async def for_token(self, user_token: str | None, /, profile: str | None = None) -> Preferences:
        """The preferences of whoever ``user_token`` belongs to, in ``profile``.

        ``None`` is no caller at all -- authentication is off -- and gets the
        configuration. ``profile`` is the one the request named; with none, the
        person's default profile is used.

        Raises:
            PreferencesUnavailableError: settings-api refused this service.
        """
        ...

    async def aclose(self) -> None:
        """Release whatever this holds open."""
        ...


def deployment_preferences(settings: Settings) -> Preferences:
    """What everybody gets when nobody's own choices are known."""
    return Preferences(
        default_model=settings.default_model,
        max_content_chars=settings.max_content_chars,
        search_backend=settings.search_backend,
        disabled_providers=frozenset(settings.disabled_providers),
        job_retention_seconds=settings.job_retention_seconds,
        default_profile=settings.keyring_default_profile,
    )


class DeploymentPreferences:
    """Everybody gets the configuration: what this service did before settings-api."""

    def __init__(self, settings: Settings) -> None:
        """Bind the configuration everybody gets."""
        self._preferences = deployment_preferences(settings)

    async def for_token(
        self, _user_token: str | None, /, profile: str | None = None
    ) -> Preferences:
        """Return the configuration; the token and the profile are ignored."""
        del profile
        return self._preferences

    async def aclose(self) -> None:
        """Nothing is held open."""


class SettingsApiPreferences:
    """Each person's own choices, read from settings-api, inside the deployment's ceilings."""

    def __init__(self, *, client: SettingsClient, settings: Settings) -> None:
        """Bind the client and the deployment ceilings it is read inside."""
        self._client = client
        self._settings = settings
        self._deployment = deployment_preferences(settings)

    async def for_token(self, user_token: str | None, /, profile: str | None = None) -> Preferences:
        """Read this person's ``search`` settings for a profile, or the configuration.

        Most ``search`` settings are profile-scoped (model, backend, safe search, result
        count, recency, language, region, pages read, summary length, research notes),
        and settings-api returns a profile's values only to a resolve that
        names it. So the profile is always named: the one the request gave, or, when it
        gave none, the person's ``common.default_profile``, which is account-wide and comes
        back from a first resolve without one.
        """
        if user_token is None:
            return self._deployment

        try:
            resolved = await self._client.resolve(NAMESPACE, user_token=user_token, profile=profile)
            chosen = profile or self._default_profile(resolved)
            if profile is None and chosen is not None:
                resolved = await self._client.resolve(
                    NAMESPACE, user_token=user_token, profile=chosen
                )
        except SettingsUnavailable:
            logger.warning("settings_unavailable", namespace=NAMESPACE)
            return Preferences(
                default_model=self._deployment.default_model,
                max_content_chars=self._deployment.max_content_chars,
                search_backend=self._deployment.search_backend,
                disabled_providers=None,
                job_retention_seconds=self._deployment.job_retention_seconds,
                default_profile=None,
                # Filtering falls back to the middle level: an outage cannot turn it off.
                safe_search=SafeSearch.MODERATE,
            )
        except SettingsRejected as error:
            logger.warning("settings_rejected", namespace=NAMESPACE, status_code=error.status_code)
            raise PreferencesUnavailableError(REFUSED) from error

        if resolved.stale:
            logger.info("settings_stale", namespace=NAMESPACE)

        return self._apply(resolved)

    async def aclose(self) -> None:
        """Close the settings-api client."""
        await self._client.aclose()

    def _apply(self, resolved: ResolvedSettings) -> Preferences:
        """Turn one person's resolved namespace into what a request runs under."""
        deployment = self._deployment
        hours = _whole_number(resolved, "job_retention_hours", minimum=0)
        chars = _whole_number(resolved, "max_content_chars", minimum=1)
        return Preferences(
            default_model=_text(resolved, "default_model") or deployment.default_model,
            max_content_chars=(
                deployment.max_content_chars
                if chars is None
                else min(deployment.max_content_chars, chars)
            ),
            search_backend=_text(resolved, "search_backend") or deployment.search_backend,
            disabled_providers=self._disabled_providers(resolved),
            job_retention_seconds=(
                deployment.job_retention_seconds if hours is None else hours * SECONDS_PER_HOUR
            ),
            default_profile=self._default_profile(resolved),
            safe_search=_safe_search(resolved),
            recency_days=_whole_number(resolved, "recency_days", minimum=1),
            language=_text(resolved, "language"),
            region=_text(resolved, "region"),
            blocked_domains=tuple(
                domain.strip().lower() for domain in _string_list(resolved, "blocked_domains") or ()
            ),
            read_top_pages=min(
                _whole_number(resolved, "read_top_pages", minimum=0) or 0,
                constants.MAX_PAGES_PER_QUERY,
            ),
            summary_length=_summary_length(resolved),
            research_notes=_text(resolved, "research_notes"),
        )

    def _disabled_providers(self, resolved: ResolvedSettings) -> frozenset[str] | None:
        """The person's list union the deployment's, or unknown.

        Reading a refused key is deferred to the operation that needs the list:
        catching :class:`SettingsRefused` here keeps a scrape that never talks
        to a model from failing because we peeked.
        """
        try:
            value = _string_list(resolved, "disabled_providers")
        except SettingsRefused:
            return None
        person = set(value or ())
        return frozenset(person) | frozenset(self._settings.disabled_providers)

    def _default_profile(self, resolved: ResolvedSettings) -> str | None:
        """``common.default_profile``: this person's, the configuration's, or unknown."""
        try:
            value = resolved.get("default_profile", None)
        except SettingsRefused:
            return None
        if isinstance(value, str) and value:
            return value
        if value is not None:
            logger.warning("setting_unusable", namespace="common", key="default_profile")
        return self._settings.keyring_default_profile


def build_preference_source(
    settings: Settings, *, client: SettingsClient | None = None
) -> PreferenceSource:
    """Choose where preferences come from, and say which in the log.

    Args:
        settings: the configuration, which also says whether settings-api is in use.
        client: substituted by tests with :class:`settings_client.testing.FakeSettingsClient`,
            and used in place of building one from ``settings``.
    """
    if client is None:
        configured = settings.settings_api
        if configured is None:
            logger.info("per_person_settings_off")
            return DeploymentPreferences(settings)
        base_url, token = configured
        client = HttpSettingsClient(base_url=base_url, service_token=token.get_secret_value())

    logger.info("per_person_settings_on", namespace=NAMESPACE)
    return SettingsApiPreferences(client=client, settings=settings)


def _safe_search(resolved: ResolvedSettings) -> SafeSearch:
    """``search.safe_search``: the person's level, or the middle one for anything else."""
    value = resolved.get("safe_search", None)
    if isinstance(value, str) and value in SafeSearch:
        return SafeSearch(value)
    if value is not None:
        logger.warning("setting_unusable", namespace=NAMESPACE, key="safe_search")
    return SafeSearch.MODERATE


def _summary_length(resolved: ResolvedSettings) -> SummaryLength:
    """``search.summary_length``: the person's length, or the standard one for anything else."""
    value = resolved.get("summary_length", None)
    if isinstance(value, str) and value in SummaryLength:
        return SummaryLength(value)
    if value is not None:
        logger.warning("setting_unusable", namespace=NAMESPACE, key="summary_length")
    return SummaryLength.STANDARD


def _whole_number(resolved: ResolvedSettings, key: str, *, minimum: int) -> int | None:
    """``key`` as a whole number no smaller than ``minimum``, or ``None`` if there is none.

    A deployment running an older settings-api may not have the key, and a value of the
    wrong shape is settings-api's bug rather than a reason to fail somebody's search.
    Either way the configuration stands in. The key is logged; the value never is.
    """
    value = resolved.get(key, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    if value is not None:
        logger.warning("setting_unusable", namespace=NAMESPACE, key=key)
    return None


def _text(resolved: ResolvedSettings, key: str) -> str | None:
    """``key`` as a non-empty string, or ``None`` if there is none usable."""
    value = resolved.get(key, None)
    if isinstance(value, str) and value:
        return value
    if value is not None:
        logger.warning("setting_unusable", namespace=NAMESPACE, key=key)
    return None


def _string_list(resolved: ResolvedSettings, key: str) -> list[str] | None:
    """``key`` as a list of strings, or ``None`` if there is none usable."""
    value = resolved.get(key, None)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    if value is not None:
        logger.warning("setting_unusable", namespace=NAMESPACE, key=key)
    return None


__all__ = [
    "PROFILE_UNKNOWN",
    "PROVIDERS_UNKNOWN",
    "REFUSED",
    "DeploymentPreferences",
    "PreferenceSource",
    "Preferences",
    "SettingsApiPreferences",
    "build_preference_source",
    "deployment_preferences",
]
