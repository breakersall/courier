from typing import Literal

from courier import message
from courier import models


class BatteryLow(models.EventBase):
    type: Literal["battery_low"] = "battery_low"


class CollarOffline(models.EventBase):
    type: Literal["collar_offline"] = "collar_offline"


def test_event_keys_suffix_repeats() -> None:
    events = [BatteryLow(target="phone"), BatteryLow(target="phone"), CollarOffline(target="phone")]
    assert message.event_keys("Rex", events) == [
        "Rex:battery_low:phone",
        "Rex:battery_low:phone:1",
        "Rex:collar_offline:phone",
    ]
