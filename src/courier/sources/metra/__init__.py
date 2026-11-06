"""Metra source: train delay, cancellation, and line service alerts from the GTFS feeds."""

import asyncio
from collections.abc import Callable
import datetime
import logging
import time
import zoneinfo

import httpx

from courier import message
from courier.sources.metra import client
from courier.sources.metra import events
from courier.sources.metra import gtfs
from courier.sources.metra import settings

log = logging.getLogger(__name__)

SCHEDULE_CHECK_INTERVAL = 3600


class MetraSource:
    """Polls Metra feeds and matches trains and lines against configured events.

    Args:
        settings: Source settings.
        http: HTTP client; the caller owns its lifecycle.
        tz: Zone for times shown in messages.
        clock: Monotonic seconds, used to pace schedule refresh checks.
    """

    def __init__(
        self,
        settings: settings.MetraSettings,
        http: httpx.AsyncClient,
        tz: zoneinfo.ZoneInfo,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._client = client.MetraClient(settings.token.get_secret_value(), http)
        self._tz = tz
        self._clock = clock
        self._schedule: gtfs.Schedule | None = None
        self._published = ""
        self._last_check = 0.0

    async def _load(self) -> gtfs.Schedule:
        """Fetches published and schedule, replacing both only if all succeed."""
        published = await self._client.fetch_published()
        wanted = {(t.line, t.number) for t in self._settings.trains}
        schedule = await asyncio.to_thread(gtfs.load_schedule, await self._client.fetch_schedule(), wanted)
        self._published, self._schedule = published, schedule
        return schedule

    def _missing_trains(self, schedule: gtfs.Schedule) -> list[settings.Train]:
        return [t for t in self._settings.trains if (t.line, t.number) not in schedule.trips]

    async def start(self) -> None:
        """Loads the schedule and verifies every configured train and line exists.

        Raises:
            SourceConfigError: A train or line is missing from the schedule.
            SourceAuthError: Metra rejected the token.
        """
        schedule = await self._load()
        self._last_check = self._clock()

        problems = []
        for t in self._missing_trains(schedule):
            numbers = sorted((n for line, n in schedule.trips if line == t.line), key=int)
            problems.append(f"{t.line} {t.number} (available on {t.line}: {', '.join(numbers) or '(none)'})")
        bad_lines = [x.id for x in self._settings.lines if x.id not in schedule.lines]
        if bad_lines:
            problems.append(f"lines {', '.join(bad_lines)} (available: {', '.join(sorted(schedule.lines))})")

        if problems:
            raise message.SourceConfigError(f"not found in schedule: {'; '.join(problems)}")

    async def _refresh(self) -> None:
        if self._clock() - self._last_check < SCHEDULE_CHECK_INTERVAL:
            return

        self._last_check = self._clock()
        try:
            if await self._client.fetch_published() != self._published:
                for t in self._missing_trains(await self._load()):
                    log.warning(
                        "Metra train %s %s is missing from the new schedule; no alerts until it returns",
                        t.line,
                        t.number,
                    )
        except (message.SourceError, httpx.HTTPError, ValueError) as exc:
            log.error("Metra schedule refresh failed, keeping old schedule: %s", exc)

    async def poll(self, now: datetime.datetime) -> list[message.Alert]:
        """Returns one alert per train or line event that matches at `now`."""
        await self._refresh()
        schedule = self._schedule
        assert schedule is not None  # noqa: S101 - start() runs first
        today = now.astimezone(gtfs.METRA_TZ).date()
        alerts = []

        if self._settings.trains:
            feed = await self._client.fetch_feed("tripupdates")
            for train in self._settings.trains:
                keys = message.event_keys(f"{train.line}{train.number}", train.events)
                for tu in events.train_trips(feed, schedule, train.line, train.number):
                    for key, event in zip(keys, train.events, strict=True):
                        if isinstance(event, settings.TrainDelayed):
                            msg = events.delay_message(tu, schedule, train.line, train.number, event, today, self._tz)
                        else:
                            msg = events.cancel_message(tu, train.line, train.number, today)
                        if msg is not None:
                            alerts.append(message.Alert(key, event, msg))

        if self._settings.lines:
            seen: set[tuple[str, str]] = set()
            feed = await self._client.fetch_feed("alerts")
            for ln in self._settings.lines:
                keys = message.event_keys(ln.id, ln.events)
                for alert_id, msg in events.alert_messages(feed, ln.id):
                    for key, event in zip(keys, ln.events, strict=True):
                        if (alert_id, event.target) in seen:  # alert tagged on several subscribed lines
                            continue
                        seen.add((alert_id, event.target))
                        alerts.append(message.Alert(f"{key}:{alert_id}", event, msg))

        return alerts
