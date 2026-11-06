"""Fi source settings: account login, collars, and their events."""

from collections.abc import Iterator
from typing import Annotated, Any, Literal, Self

import pydantic

from courier import models


class BatteryLow(models.EventBase):
    """Fires while battery is below `threshold` percent.

    Attributes:
        type: Always "battery_low".
        threshold: Percent; fires when battery is strictly below it.
        suppress_when_charging: Skip while the collar is charging or on its base.
    """

    type: Literal["battery_low"]
    threshold: int = pydantic.Field(default=40, ge=1, le=100)
    suppress_when_charging: bool = False


class CollarOffline(models.EventBase):
    """Fires when the collar has not connected for more than `minutes`.

    Attributes:
        type: Always "collar_offline".
        minutes: Allowed silence before firing.
    """

    type: Literal["collar_offline"]
    minutes: int = pydantic.Field(default=60, ge=1)


class LostMode(models.EventBase):
    """Fires while the collar is in lost dog mode.

    Attributes:
        type: Always "lost_mode".
    """

    type: Literal["lost_mode"]


FiEvent = Annotated[BatteryLow | CollarOffline | LostMode, pydantic.Field(discriminator="type")]


class Collar(models.Model):
    """A pet's collar and its event subscriptions.

    Attributes:
        name: Pet name exactly as shown in the Fi app.
        events: Subscriptions evaluated on every poll.
    """

    name: str
    events: list[FiEvent]


class FiSettings(models.SourceSettings):
    """Fi source settings.

    Attributes:
        type: Always "fi".
        email: Account email.
        password: Account password.
        collars: Collars to monitor.
    """

    type: Literal["fi"]
    email: str
    password: pydantic.SecretStr
    collars: list[Collar]

    @pydantic.model_validator(mode="before")
    @classmethod
    def _secrets(cls, data: Any) -> Any:
        return models.resolve_secret(data, "password", required=True)

    @pydantic.model_validator(mode="after")
    def _unique_collars(self) -> Self:
        names = [c.name for c in self.collars]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate collar names: {', '.join(dupes)}")
        return self

    def events(self) -> Iterator[models.EventBase]:
        """Yields every collar event."""
        for collar in self.collars:
            yield from collar.events
