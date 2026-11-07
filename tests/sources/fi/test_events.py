import dataclasses
import datetime
from typing import Any
import zoneinfo

from courier import message
from courier.sources.fi import client
from courier.sources.fi import events
from courier.sources.fi import settings

NY = zoneinfo.ZoneInfo("America/New_York")
LAST = datetime.datetime(2026, 10, 9, 14, 2, tzinfo=datetime.UTC)
NOW = LAST + datetime.timedelta(minutes=72)


BASE = client.PetStatus(
    pet_id="p",
    name="Rex",
    battery_percent=37,
    is_charging=None,
    connection_type="ConnectedToUser",
    last_connection=LAST,
    mode="NORMAL",
)


def pet(**kw: Any) -> client.PetStatus:
    return dataclasses.replace(BASE, **kw)


def battery(**kw: Any) -> settings.BatteryLow:
    return settings.BatteryLow.model_validate({"type": "battery_low", "target": "t"} | kw)


def test_battery_low_matches() -> None:
    assert events.evaluate(pet(), battery(), NOW, NY) == (
        message.Message(
            title="Rex: Battery Low",
            body="Rex's collar battery is 37% (threshold 40%).",
            priority="default",
            tags="battery",
            sid="p-battery_low",
        ),
        "battery 37% < threshold 40%",
    )


def test_battery_at_threshold_no_match() -> None:
    assert events.evaluate(pet(battery_percent=40), battery(), NOW, NY) == (None, "battery 40% ≥ threshold 40%")


def test_battery_suppressed_when_charging_v2() -> None:
    on_base = pet(connection_type="ConnectedToBase")
    assert events.evaluate(on_base, battery(suppress_when_charging=True), NOW, NY) == (
        None,
        "battery 37% < threshold 40%, but charging",
    )


def test_battery_suppressed_v1_is_charging() -> None:
    message_sent, _ = events.evaluate(pet(is_charging=True), battery(suppress_when_charging=True), NOW, NY)
    assert message_sent is None


def test_battery_not_suppressed_without_flag() -> None:
    message_sent, _ = events.evaluate(pet(connection_type="ConnectedToBase"), battery(), NOW, NY)
    assert message_sent is not None


def test_offline_matches() -> None:
    event = settings.CollarOffline(type="collar_offline", target="t", minutes=60)

    assert events.evaluate(pet(), event, NOW, NY) == (
        message.Message(
            title="Rex: Collar Offline",
            body="No connection for 72 minutes (last seen 10:02).",
            priority="high",
            tags="warning",
            sid="p-collar_offline",
        ),
        "offline 72 min > 60",
    )


def test_offline_shows_date_after_a_day() -> None:
    event = settings.CollarOffline(type="collar_offline", target="t", minutes=60)
    message_sent, _ = events.evaluate(pet(), event, LAST + datetime.timedelta(days=2), NY)
    assert message_sent is not None
    assert message_sent.body.endswith("(last seen Fri Oct 09 10:02).")


def test_offline_within_limit_none() -> None:
    event = settings.CollarOffline(type="collar_offline", target="t", minutes=90)
    assert events.evaluate(pet(), event, NOW, NY) == (None, "offline 72 min ≤ 90")


def test_lost_mode() -> None:
    event = settings.LostMode(type="lost_mode", target="t")

    assert events.evaluate(pet(), event, NOW, NY) == (None, "mode is normal")
    assert events.evaluate(pet(mode="LOST_DOG"), event, NOW, NY) == (
        message.Message(
            title="Rex: Lost Mode",
            body="Lost dog mode is active.",
            priority="urgent",
            tags="rotating_light",
            sid="p-lost_mode",
        ),
        "mode is lost dog",
    )
