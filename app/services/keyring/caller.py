"""Who a request is being made for.

Credentials in keyring belong to a person, not to this service, so almost
everything below the HTTP layer needs to know which person it is acting for.
That identity travels as a :class:`Caller`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Caller:
    """The end user a request is being made on behalf of."""

    account_id: str
    """Opaque account id, taken from the verified token's ``sub`` claim.

    Used as a cache key. Never logged alongside anything that would identify the
    person - keyring deliberately keeps email addresses out of these tokens.
    """

    profile: str
    """Which of the account's credential sets to draw from, e.g. ``personal``."""

    user_token: str
    """The short-lived token, forwarded to keyring on each resolve."""

    @property
    def cache_key(self) -> tuple[str, str]:
        """Identity for caching, stable across token rotation.

        Deliberately not the token: tokens live minutes, so keying on one would
        throw the catalogue away every time a caller refreshed it.
        """
        return (self.account_id, self.profile)
