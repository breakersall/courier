"""Metra source settings: trains, lines, and their events."""

from collections.abc import Iterator
from typing import Annotated, Any, Literal, Self

import pydantic

from courier import models


class TrainDelayed(models.EventBase):
    """Fires while a train is delayed by at least `minutes`.

    Attributes:
        type: Always "train_delayed".
        minutes: Minimum delay before firing.
    """

    type: Literal["train_delayed"]
    minutes: int = pydantic.Field(default=5, ge=1)


class TrainCancelled(models.EventBase):
    """Fires when a train is cancelled.

    Attributes:
        type: Always "train_cancelled".
    """

    type: Literal["train_cancelled"]


class ServiceAlert(models.EventBase):
    """Fires when Metra posts a service alert for a line.

    Attributes:
        type: Always "service_alert".
    """

    type: Literal["service_alert"]


TrainEvent = Annotated[TrainDelayed | TrainCancelled, pydantic.Field(discriminator="type")]


class Train(models.Model):
    """A scheduled train and its event subscriptions.

    Attributes:
        line: Metra line id, e.g. "UP-N".
        number: Train number, e.g. "310".
        events: Subscriptions evaluated on every poll.
    """

    line: str
    number: str
    events: list[TrainEvent]


class Line(models.Model):
    """A line and its alert subscriptions.

    Attributes:
        id: Metra line id.
        events: Subscriptions evaluated on every poll.
    """

    id: str
    events: list[ServiceAlert]


class MetraSettings(models.SourceSettings):
    """Metra source settings.

    Attributes:
        type: Always "metra".
        token: Metra API token.
        poll_interval: Seconds between polls.
        trains: Trains to monitor.
        lines: Lines whose alerts to monitor.
    """

    type: Literal["metra"]
    token: pydantic.SecretStr
    poll_interval: int = pydantic.Field(default=120, ge=60)
    trains: list[Train] = pydantic.Field(default_factory=list)
    lines: list[Line] = pydantic.Field(default_factory=list)

    @pydantic.model_validator(mode="before")
    @classmethod
    def _secrets(cls, data: Any) -> Any:
        return models.resolve_secret(data, "token", required=True)

    @pydantic.model_validator(mode="after")
    def _unique(self) -> Self:
        trains = [f"{train.line} {train.number}" for train in self.trains]
        lines = [line.id for line in self.lines]
        dupes = models.duplicates(trains) + models.duplicates(lines)
        if dupes:
            raise ValueError(f"duplicate trains or lines: {', '.join(dupes)}")

        return self

    def events(self) -> Iterator[models.EventBase]:
        """Yields every train and line event."""
        for train in self.trains:
            yield from train.events
        for line in self.lines:
            yield from line.events
