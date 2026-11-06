import datetime
import zoneinfo

from courier import limiter
from courier import models

NY = zoneinfo.ZoneInfo("America/New_York")
DAY = models.Window(start=datetime.time(8), end=datetime.time(20))


def at(hh: int, mm: int = 0, day: int = 9, month: int = 10) -> datetime.datetime:
    return datetime.datetime(2026, month, day, hh, mm, tzinfo=NY)


def test_window_bounds() -> None:
    assert limiter.window_bounds(at(9), DAY) == (at(8), at(20))
    assert limiter.window_bounds(at(7, 59), DAY) is None
    assert limiter.window_bounds(at(20), DAY) is None
    assert limiter.window_bounds(at(23, 59), None) == (at(0), at(0, day=10))


def test_n2_from_0800() -> None:
    s = limiter.record(limiter.LimiterState(), at(8), DAY, 2)
    assert s.next_allowed == at(14)
    assert limiter.allowed(s, at(13, 59), DAY, 2) is False
    assert limiter.allowed(s, at(14), DAY, 2) is True

    s = limiter.record(s, at(14), DAY, 2)
    assert limiter.allowed(s, at(19), DAY, 2) is False


def test_n2_first_match_1800() -> None:
    s = limiter.record(limiter.LimiterState(), at(18), DAY, 2)
    assert s.next_allowed == at(19)


def test_n3_even_slots() -> None:
    s = limiter.record(limiter.LimiterState(), at(8), DAY, 3)
    assert s.next_allowed == at(12)
    s = limiter.record(s, at(12), DAY, 3)
    assert s.next_allowed == at(16)


def test_shared_budget_after_clear() -> None:
    s = limiter.record(limiter.LimiterState(), at(8), DAY, 2)
    assert limiter.allowed(s, at(17), DAY, 2) is True
    s = limiter.record(s, at(17), DAY, 2)
    assert limiter.allowed(s, at(19, 30), DAY, 2) is False


def test_outside_window_denied() -> None:
    assert limiter.allowed(limiter.LimiterState(), at(7, 59), DAY, 1) is False
    assert limiter.allowed(limiter.LimiterState(), at(20), DAY, 2) is False


def test_day_rollover_resets() -> None:
    s = limiter.record(limiter.LimiterState(), at(8), DAY, 1)
    assert limiter.allowed(s, at(19), DAY, 1) is False
    assert limiter.allowed(s, at(8, day=10), DAY, 1) is True
    s = limiter.record(s, at(8, day=10), DAY, 1)
    assert s.sent == 1


def test_no_window_full_day() -> None:
    s = limiter.record(limiter.LimiterState(), at(0), None, 2)
    assert s.next_allowed == at(12)


def test_dst_spring_forward() -> None:
    # On 2026-03-08 02:00 EST jumps to 03:00 EDT, so 01:00 to 05:00 local is 3 real hours
    w = models.Window(start=datetime.time(1), end=datetime.time(5))
    s = limiter.record(limiter.LimiterState(), at(1, day=8, month=3), w, 2)
    assert s.next_allowed == datetime.datetime(2026, 3, 8, 7, 30, tzinfo=datetime.UTC)


def test_window_days() -> None:
    weekdays = models.Window(start=datetime.time(8), end=datetime.time(20), days=["mon", "fri"])
    assert limiter.window_bounds(at(9), weekdays) == (at(8), at(20))  # Fri
    assert limiter.window_bounds(at(9, day=10), weekdays) is None  # Sat
    assert limiter.allowed(limiter.LimiterState(), at(9, day=10), weekdays, 1) is False
