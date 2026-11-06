"""Event evaluators: decide whether a pet's status matches a subscription."""

import dataclasses
import datetime
from typing import assert_never
import zoneinfo

from courier import message
from courier.sources.fi import client
from courier.sources.fi import settings


def _battery_low(pet: client.PetStatus, event: settings.BatteryLow) -> message.Message | None:
    if pet.battery_percent >= event.threshold or (event.suppress_when_charging and pet.charging):
        return None

    return message.Message(
        f"{pet.name}: Battery Low",
        f"{pet.name}'s collar battery is {pet.battery_percent}% (threshold {event.threshold}%).",
        "default",
        "battery",
    )


def _collar_offline(
    pet: client.PetStatus, event: settings.CollarOffline, now: datetime.datetime, tz: zoneinfo.ZoneInfo
) -> message.Message | None:
    minutes = int((now - pet.last_connection).total_seconds() // 60)

    if minutes <= event.minutes:
        return None

    local = pet.last_connection.astimezone(tz)
    seen = local.strftime("%H:%M" if local.date() == now.astimezone(tz).date() else "%a %b %d %H:%M")

    return message.Message(
        f"{pet.name}: Collar Offline",
        f"No connection for {minutes} minutes (last seen {seen}).",
        "high",
        "warning",
    )


def _lost_mode(pet: client.PetStatus) -> message.Message | None:
    if pet.mode != "LOST_DOG":
        return None

    return message.Message(f"{pet.name}: Lost Mode", "Lost dog mode is active.", "urgent", "rotating_light")


def evaluate(
    pet: client.PetStatus, event: settings.FiEvent, now: datetime.datetime, tz: zoneinfo.ZoneInfo
) -> message.Message | None:
    """Evaluates one subscription against a pet's status.

    Args:
        pet: Current pet status.
        event: The subscription to check.
        now: Current time (aware).
        tz: Zone for times shown in messages.

    Returns:
        The message to send if the event matches, else None. Priority is the source default;
        `loop.deliver` applies the event's override.
    """
    match event:
        case settings.BatteryLow():
            msg = _battery_low(pet, event)
        case settings.CollarOffline():
            msg = _collar_offline(pet, event, now, tz)
        case settings.LostMode():
            msg = _lost_mode(pet)
        case _:
            assert_never(event)

    if msg is None:
        return None

    return dataclasses.replace(msg, sid=f"{pet.pet_id}-{event.type}")
