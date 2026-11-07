"""Time-window gate and "N per remaining window" rate limiter.

All functions are pure. `t` must be aware and in the configured timezone: its
local date picks the window.
"""

import dataclasses
import datetime

from courier import models


@dataclasses.dataclass(frozen=True, slots=True)
class LimiterState:
    """Rate-limit bookkeeping for one (source name, check key) pair.

    Attributes:
        window_date: Local date the counters apply to.
        sent: Notifications sent on `window_date`.
        next_allowed: Earliest time the next notification may be sent.
    """

    window_date: datetime.date | None = None
    sent: int = 0
    next_allowed: datetime.datetime | None = None


def window_bounds(
    t: datetime.datetime, window: models.Window | None
) -> tuple[datetime.datetime, datetime.datetime] | None:
    """Returns the window on `t`'s local date as [start, end), or None if `t` is outside it or on an excluded weekday.

    Args:
        t: Aware time in the configured timezone.
        window: Delivery window; None means the whole day.
    """
    day = t.date()
    if window is None:
        start = datetime.datetime.combine(day, datetime.time(0), tzinfo=t.tzinfo)
        end = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(0), tzinfo=t.tzinfo)
    elif window.days is not None and day.weekday() not in window.days:
        return None
    else:
        start = datetime.datetime.combine(day, window.start, tzinfo=t.tzinfo)
        end = datetime.datetime.combine(day, window.end, tzinfo=t.tzinfo)

    if not start <= t < end:
        return None

    return (start, end)


def _today(state: LimiterState, t: datetime.datetime) -> LimiterState:
    if state.window_date == t.date():
        return state

    return LimiterState(window_date=t.date())


def blocked(state: LimiterState, t: datetime.datetime, window: models.Window | None, max_per_window: int) -> str | None:
    """Returns why a notification may not be sent at `t`, or None if it may.

    Args:
        state: Current bookkeeping for the event.
        t: Aware time in the configured timezone.
        window: Delivery window; None means the whole day.
        max_per_window: Cap on sends per window day.

    Returns:
        A one-line reason, or None.
    """
    if window is not None and window_bounds(t, window) is None:
        if window.days is not None and t.weekday() not in window.days:
            return f"not on {t:%A}"
        return f"outside window {window.start:%H:%M}–{window.end:%H:%M}"  # noqa: RUF001 - en dash for a time range

    state = _today(state, t)
    if state.sent >= max_per_window:
        return f"budget used: {state.sent} of {max_per_window} today"

    if state.next_allowed is not None and t < state.next_allowed:
        return f"next allowed {state.next_allowed.astimezone(t.tzinfo):%H:%M}"

    return None


def record(
    state: LimiterState, t: datetime.datetime, window: models.Window | None, max_per_window: int
) -> LimiterState:
    """Accounts for a successful send at `t`.

    The next send is allowed after the remaining window is split evenly among
    the remaining sends.

    Args:
        state: Current bookkeeping for the event.
        t: Aware time of the send, in the configured timezone.
        window: Delivery window; None means the whole day.
        max_per_window: Cap on sends per window day.

    Returns:
        The updated state.
    """
    state = _today(state, t)
    sent = state.sent + 1
    bounds = window_bounds(t, window)

    if sent >= max_per_window or bounds is None:
        return dataclasses.replace(state, sent=sent, next_allowed=None)

    # same-tzinfo subtraction ignores DST, so do slot math in UTC
    now, end = t.astimezone(datetime.UTC), bounds[1].astimezone(datetime.UTC)
    return dataclasses.replace(state, sent=sent, next_allowed=now + (end - now) / (max_per_window - sent + 1))
