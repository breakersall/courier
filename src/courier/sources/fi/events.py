"""Event evaluators: decide whether a pet's status matches a subscription."""

import dataclasses
import datetime
from typing import assert_never
import zoneinfo

from courier import message
from courier.sources.fi import client
from courier.sources.fi import settings


def _battery_low(pet: client.PetStatus, event: settings.BatteryLow) -> message.Decision:
    level = f"battery {pet.battery_percent}%"

    if pet.battery_percent >= event.threshold:
        return None, f"{level} ≥ threshold {event.threshold}%"

    if event.suppress_when_charging and pet.charging:
        return None, f"{level} < threshold {event.threshold}%, but charging"

    low_battery = message.Message(
        f"{pet.name}: Battery Low",
        f"{pet.name}'s collar battery is {pet.battery_percent}% (threshold {event.threshold}%).",
        "default",
        "battery",
    )
    return low_battery, f"{level} < threshold {event.threshold}%"


def _collar_offline(
    pet: client.PetStatus, event: settings.CollarOffline, now: datetime.datetime, tz: zoneinfo.ZoneInfo
) -> message.Decision:
    minutes_offline = int((now - pet.last_connection).total_seconds() // 60)

    if minutes_offline <= event.minutes:
        return None, f"offline {minutes_offline} min ≤ {event.minutes}"

    offline = message.Message(
        f"{pet.name}: Collar Offline",
        f"No connection for {minutes_offline} minutes (last seen {_last_seen(pet, now, tz)}).",
        "high",
        "warning",
    )
    return offline, f"offline {minutes_offline} min > {event.minutes}"


def _lost_mode(pet: client.PetStatus) -> message.Decision:
    mode = f"mode is {pet.mode.lower().replace('_', ' ')}"

    if pet.mode != "LOST_DOG":
        return None, mode

    lost = message.Message(f"{pet.name}: Lost Mode", "Lost dog mode is active.", "urgent", "rotating_light")
    return lost, mode


def _last_seen(pet: client.PetStatus, now: datetime.datetime, tz: zoneinfo.ZoneInfo) -> str:
    """Returns when the collar last connected: a time today, else weekday, date and time."""
    local = pet.last_connection.astimezone(tz)

    if local.date() == now.astimezone(tz).date():
        return local.strftime("%H:%M")

    return local.strftime("%a %b %d %H:%M")


def evaluate(
    pet: client.PetStatus, event: settings.FiEvent, now: datetime.datetime, tz: zoneinfo.ZoneInfo
) -> message.Decision:
    """Evaluates one subscription against a pet's status.

    Args:
        pet: Current pet status.
        event: The subscription to check.
        now: Current time (aware).
        tz: Zone for times shown in messages.

    Returns:
        The message to send if the event matches, else None, and a one-line reason either way.
        Priority is the source default; `loop.deliver` applies the event's override.
    """
    match event:
        case settings.BatteryLow():
            matched, reason = _battery_low(pet, event)
        case settings.CollarOffline():
            matched, reason = _collar_offline(pet, event, now, tz)
        case settings.LostMode():
            matched, reason = _lost_mode(pet)
        case _:
            assert_never(event)

    if matched is None:
        return None, reason

    return dataclasses.replace(matched, sid=f"{pet.pet_id}-{event.type}"), reason
