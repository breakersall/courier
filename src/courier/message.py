"""Source-agnostic message protocol shared by all sources and the notifier."""

import collections
from collections.abc import Sequence
import dataclasses
import datetime
from typing import Protocol

from courier import models


@dataclasses.dataclass(frozen=True, slots=True)
class Message:
    """A notification to send.

    Attributes:
        title: Short headline.
        body: Message text.
        priority: ntfy priority name.
        tags: Comma-separated ntfy tags.
        sid: Sequence id, so repeats replace the previous notification.
    """

    title: str
    body: str
    priority: str
    tags: str
    sid: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class Alert:
    """A message plus the event that produced it.

    Attributes:
        key: Stable limiter-state id for the event.
        event: The configured event that matched.
        message: The notification to send.
    """

    key: str
    event: models.EventBase
    message: Message


class Source(Protocol):
    """A notification source."""

    async def start(self) -> None:
        """Validates configuration against the upstream service."""
        ...

    async def poll(self, now: datetime.datetime) -> list[Alert]:
        """Returns alerts for events matching at `now`."""
        ...


class SourceError(Exception):
    """A source failed to fetch or parse upstream data."""


class SourceAuthError(SourceError):
    """A source's credentials were rejected."""


def event_keys(entity: str, events: Sequence[models.EventBase]) -> list[str]:
    """Returns a stable limiter-state id per event, parallel to `events`.

    Ids survive reordering; repeated type/target pairs get a numeric suffix.
    """
    seen: collections.Counter[str] = collections.Counter()
    keys = []

    for event in events:
        base = f"{entity}:{event.type}:{event.target}"
        keys.append(f"{base}:{seen[base]}" if seen[base] else base)
        seen[base] += 1

    return keys
