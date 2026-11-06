"""Minimal async client for the Metra GTFS realtime and static feeds."""

import logging
from typing import Literal

from google.protobuf import message as pb_message
from google.transit import gtfs_realtime_pb2
import httpx
import prometheus_client

from courier import message
from courier import metrics

log = logging.getLogger(__name__)

REALTIME_URL = "https://gtfspublic.metrarr.com/gtfs/public"
SCHEDULE_URL = "https://schedules.metrarail.com/gtfs/schedule.zip"
PUBLISHED_URL = "https://schedules.metrarail.com/gtfs/published.txt"
RATE_LIMIT_WARN_BELOW = 20

RATELIMIT_REMAINING = prometheus_client.Gauge(
    "courier_metra_ratelimit_remaining",
    "Metra realtime requests remaining, from the last response.",
    registry=metrics.REGISTRY,
)


class MetraClient:
    """Fetches Metra realtime feeds and static schedule files."""

    def __init__(self, token: str, http: httpx.AsyncClient) -> None:
        self._token = token
        self._http = http

    async def _get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        try:
            resp = await self._http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise message.SourceError(f"Metra request failed: {type(exc).__name__}") from None

        if resp.status_code in (401, 403):
            raise message.SourceAuthError(f"Metra rejected the token (HTTP {resp.status_code})")
        if not resp.is_success:
            raise message.SourceError(f"Metra returned HTTP {resp.status_code}")

        return resp

    async def fetch_feed(self, name: Literal["tripupdates", "alerts", "positions"]) -> gtfs_realtime_pb2.FeedMessage:
        """Fetches and decodes a realtime feed.

        Raises:
            SourceAuthError: The token was rejected.
            SourceError: Any other HTTP failure, or an undecodable body.
        """
        resp = await self._get(f"{REALTIME_URL}/{name}", {"Authorization": f"Bearer {self._token}"})
        remaining = resp.headers.get("x-ratelimit-remaining", "")
        if remaining.isdigit():
            RATELIMIT_REMAINING.set(int(remaining))

            if int(remaining) < RATE_LIMIT_WARN_BELOW:
                log.warning("Metra rate limit nearly exhausted: %s requests remaining", remaining)

        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            feed.ParseFromString(resp.content)
        except pb_message.DecodeError:
            raise message.SourceError(f"Metra {name} feed is not valid protobuf") from None

        return feed

    async def fetch_published(self) -> str:
        """Returns the static schedule's published timestamp text."""
        return (await self._get(PUBLISHED_URL)).text.strip()

    async def fetch_schedule(self) -> bytes:
        """Returns the static schedule zip."""
        return (await self._get(SCHEDULE_URL)).content
