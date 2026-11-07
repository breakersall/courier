"""Fi (tryfi.com) source: collar battery, connectivity, and lost-mode alerts."""

import datetime
import logging
import zoneinfo

import httpx

from courier import message
from courier.sources.fi import client
from courier.sources.fi import events
from courier.sources.fi import settings

log = logging.getLogger(__name__)


class FiSource:
    """Polls Fi and matches pets against configured collar events.

    Args:
        settings: Source settings.
        http: HTTP client; the caller owns its lifecycle.
        tz: Zone for times shown in messages.
    """

    def __init__(self, settings: settings.FiSettings, http: httpx.AsyncClient, tz: zoneinfo.ZoneInfo) -> None:
        self._settings = settings
        self._client = client.FiClient(settings.email, settings.password.get_secret_value(), http)
        self._tz = tz

    async def start(self) -> None:
        """Fetches pets once and verifies every configured collar exists.

        Raises:
            SourceConfigError: A configured collar is missing; the message lists available names.
            SourceAuthError: Fi rejected the login.
        """
        await self._client.fetch_pets()
        names = self._client.pet_names
        missing = [c.name for c in self._settings.collars if c.name not in names]

        if missing:
            available = ", ".join(sorted(names)) or "(none)"
            raise message.SourceConfigError(
                f"collars not found on Fi account: {', '.join(missing)}; available: {available}"
            )

    async def poll(self, now: datetime.datetime) -> list[message.Check]:
        """Returns each configured collar event evaluated at `now`."""
        pets = {p.name: p for p in await self._client.fetch_pets()}
        checks = []

        for collar in self._settings.collars:
            pet = pets.get(collar.name)
            if pet is None:
                log.warning("collar %r missing from Fi response", collar.name)
                continue

            for key, event in zip(message.event_keys(collar.name, collar.events), collar.events, strict=True):
                matched, reason = events.evaluate(pet, event, now, self._tz)
                checks.append(message.Check(key, event, matched, reason))

        return checks
