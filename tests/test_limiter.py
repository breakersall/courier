import datetime
import zoneinfo

from courier import limiter
from courier import models

NY = zoneinfo.ZoneInfo("America/New_York")
DAY = models.Window(start=datetime.time(8), end=datetime.time(20))
WEEKDAYS = models.Window(start=datetime.time(8), end=datetime.time(20), days=["mon", "tue", "wed", "thu", "fri"])


def at(hh: int, mm: int = 0, day: int = 9, month: int = 10) -> datetime.datetime:
    return datetime.datetime(2026, month, day, hh, mm, tzinfo=NY)


def test_window_edges() -> None:
    assert limiter.blocked(limiter.LimiterState(), at(8), DAY, 1) is None
    assert limiter.blocked(limiter.LimiterState(), at(7, 59), DAY, 1) is not None
    assert limiter.blocked(limiter.LimiterState(), at(20), DAY, 1) is not None
    assert limiter.blocked(limiter.LimiterState(), at(23, 59), None, 1) is None


def test_n2_from_0800() -> None:
    s = limiter.record(limiter.LimiterState(), at(8), DAY, 2)
    assert s.next_allowed == at(14)
    assert limiter.blocked(s, at(13, 59), DAY, 2) is not None
    assert limiter.blocked(s, at(14), DAY, 2) is None

    s = limiter.record(s, at(14), DAY, 2)
    assert limiter.blocked(s, at(19), DAY, 2) is not None


def test_n2_first_match_1800() -> None:
    s = limiter.record(limiter.LimiterState(), at(18), DAY, 2)
    assert s.next_allowed == at(19)


def test_n3_even_slots() -> None:
    s = limiter.record(limiter.LimiterState(), at(8), DAY, 3)
    assert s.next_allowed == at(12)
    s = limiter.record(s, at(12), DAY, 3)
    assert s.next_allowed == at(16)


def test_day_rollover_resets() -> None:
    s = limiter.record(limiter.LimiterState(), at(8), DAY, 1)
    assert limiter.blocked(s, at(19), DAY, 1) is not None
    assert limiter.blocked(s, at(8, day=10), DAY, 1) is None
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


FRESH = limiter.LimiterState()


def test_blocked_outside_window() -> None:
    assert limiter.blocked(FRESH, at(21), DAY, 1) == "outside window 08:00–20:00"  # noqa: RUF001


def test_blocked_excluded_weekday() -> None:
    friday = at(9)
    saturday = at(9, day=10)
    assert limiter.blocked(FRESH, friday, WEEKDAYS, 1) is None
    assert limiter.blocked(FRESH, saturday, WEEKDAYS, 1) == "not on Saturday"


def test_blocked_budget_used() -> None:
    one_sent = limiter.LimiterState(window_date=at(9).date(), sent=1)
    assert limiter.blocked(one_sent, at(9), None, 1) == "budget used: 1 of 1 today"


def test_blocked_next_allowed() -> None:
    next_at_14 = limiter.LimiterState(window_date=at(9).date(), sent=1, next_allowed=at(14))
    assert limiter.blocked(next_at_14, at(9), None, 2) == "next allowed 14:00"


def test_blocked_none_when_allowed() -> None:
    assert limiter.blocked(FRESH, at(9), None, 1) is None
