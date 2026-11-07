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


type Decision = tuple[Message | None, str]
"""An evaluator's result: the message if the event matched, else None, and a one-line reason."""


@dataclasses.dataclass(frozen=True, slots=True)
class Check:
    """One configured event evaluated against upstream data.

    Attributes:
        key: Stable limiter-state id for the event.
        event: The configured event.
        message: The notification to send if the event matched, else None.
        reason: One line explaining the decision, match or not.
    """

    key: str
    event: models.EventBase
    message: Message | None
    reason: str


class Source(Protocol):
    """A notification source."""

    async def start(self) -> None:
        """Validates configuration against the upstream service."""
        ...

    async def poll(self, now: datetime.datetime) -> list[Check]:
        """Returns every configured event evaluated at `now`."""
        ...


class SourceError(Exception):
    """A source failed to fetch or parse upstream data."""


class SourceAuthError(SourceError):
    """A source's credentials were rejected."""


class SourceConfigError(SourceError):
    """A source's configuration does not match the upstream service, e.g. an unknown collar or train."""


def event_keys(entity: str, events: Sequence[models.EventBase]) -> list[str]:
    """Returns a stable limiter-state id per event, parallel to `events`.

    Ids survive reordering; repeated type/target pairs get a numeric suffix.
    """
    seen: collections.Counter[str] = collections.Counter()
    keys = []

    for event in events:
        base = f"{entity}:{event.type}:{event.target}"
        if seen[base]:
            keys.append(f"{base}:{seen[base]}")
        else:
            keys.append(base)

        seen[base] += 1

    return keys
