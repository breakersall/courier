"""Time-window gate and "N per remaining window" rate limiter.

All functions are pure. `t` must be aware and in the configured timezone: its
local date picks the window.
"""

import dataclasses
import datetime

from courier import models


@dataclasses.dataclass(frozen=True, slots=True)
class LimiterState:
    """Rate-limit bookkeeping for one (source name, alert key) pair.

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

    return (start, end) if start <= t < end else None


def _today(state: LimiterState, t: datetime.datetime) -> LimiterState:
    return state if state.window_date == t.date() else LimiterState(window_date=t.date())


def allowed(state: LimiterState, t: datetime.datetime, window: models.Window | None, max_per_window: int) -> bool:
    """Returns whether a notification may be sent at `t`.

    Args:
        state: Current bookkeeping for the alert.
        t: Aware time in the configured timezone.
        window: Delivery window; None means the whole day.
        max_per_window: Cap on sends per window day.
    """
    if window_bounds(t, window) is None:
        return False

    state = _today(state, t)
    if state.sent >= max_per_window:
        return False

    return state.next_allowed is None or t >= state.next_allowed


def record(
    state: LimiterState, t: datetime.datetime, window: models.Window | None, max_per_window: int
) -> LimiterState:
    """Accounts for a successful send at `t`.

    The next send is allowed after the remaining window is split evenly among
    the remaining sends.

    Args:
        state: Current bookkeeping for the alert.
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
