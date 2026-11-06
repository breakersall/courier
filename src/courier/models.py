"""Base configuration models shared by every source.

Secrets may be given inline (`x`) or read from a file (`x_file`), which is how
Kubernetes Secrets are mounted.
"""

from collections.abc import Iterator
import datetime
import pathlib
from typing import Any, Literal, Self

import pydantic

Priority = Literal["min", "low", "default", "high", "urgent"]


def resolve_secret(data: Any, key: str, *, required: bool) -> Any:
    """Replaces `<key>_file` in raw model input with the file's stripped contents under `key`.

    Args:
        data: Raw model input; returned unchanged if it is not a dict.
        key: Secret field name.
        required: Whether one of `key` or `<key>_file` must be present.

    Returns:
        The input with any `<key>_file` entry replaced by `key`.

    Raises:
        ValueError: Both or (if required) neither form is set, or the file is unreadable.
    """
    if not isinstance(data, dict):
        return data

    file_key = f"{key}_file"

    if key in data and file_key in data:
        raise ValueError(f"set only one of {key!r} or {file_key!r}")

    if file_key in data:
        if not isinstance(data[file_key], str):
            raise ValueError(f"{file_key} must be a file path")

        path = pathlib.Path(data[file_key])

        try:
            secret = path.read_text().strip()
        except OSError as exc:
            raise ValueError(f"cannot read {file_key} {path}: {exc.strerror}") from None

        if not secret:
            raise ValueError(f"{file_key} {path} is empty")

        data = {k: v for k, v in data.items() if k != file_key} | {key: secret}

    if required and key not in data:
        raise ValueError(f"{key!r} or {file_key!r} is required")

    return data


class Model(pydantic.BaseModel):
    """Frozen base model that rejects unknown keys and hides input values in errors."""

    model_config = pydantic.ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class Window(Model):
    """Delivery window, in the configured timezone.

    Attributes:
        start: First time of day notifications may be sent.
        end: Exclusive end; must be after `start` (midnight-crossing is not supported).
        days: Weekdays the window applies on (0 = Monday), given as names like "mon"; None means every day.
    """

    start: datetime.time
    end: datetime.time
    days: frozenset[int] | None = None

    @pydantic.field_validator("days", mode="before")
    @classmethod
    def _day_names(cls, value: Any) -> Any:
        if value is None:
            return None
        if not value:
            raise ValueError("days must not be empty; omit it for every day")
        if isinstance(value, str):
            value = [value]

        names = [str(d).lower() for d in value]
        unknown = [d for d in names if d not in _DAYS]
        if unknown:
            raise ValueError(f"unknown days {unknown}; use {', '.join(_DAYS)}")

        return frozenset(_DAYS.index(d) for d in names)

    @pydantic.model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("window start must be before end (midnight-crossing not supported)")
        return self


class EventBase(Model):
    """Fields common to every configured event.

    Attributes:
        type: Event kind; subclasses narrow it to a `Literal`.
        target: Name of the target to deliver to.
        max_per_window: Cap on notifications per delivery window while the event keeps matching.
        priority: ntfy priority override; None means the source default.
    """

    type: str
    target: str
    max_per_window: int = pydantic.Field(default=1, ge=1)
    priority: Priority | None = None


class SourceSettings(Model):
    """Settings common to every source.

    Attributes:
        poll_interval: Seconds between polls; None means the source default.
    """

    poll_interval: int | None = pydantic.Field(default=None, ge=60)

    def events(self) -> Iterator[EventBase]:
        """Yields the events configured for this source.

        Raises:
            NotImplementedError: Subclasses must override this.
        """
        raise NotImplementedError
