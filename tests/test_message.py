from courier import message
from courier.sources.fi import settings as fi_settings


def test_event_keys_suffix_repeats() -> None:
    battery_low = fi_settings.BatteryLow(type="battery_low", target="phone")
    collar_offline = fi_settings.CollarOffline(type="collar_offline", target="phone")

    assert message.event_keys("Rex", [battery_low, battery_low, collar_offline]) == [
        "Rex:battery_low:phone",
        "Rex:battery_low:phone:1",
        "Rex:collar_offline:phone",
    ]
