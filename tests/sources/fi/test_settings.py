import pathlib
from typing import Any

import pydantic
import pytest

from courier.sources.fi import settings


def raw(**kw: Any) -> dict[str, Any]:
    return {
        "type": "fi",
        "email": "me@example.com",
        "password": "pw",
        "collars": [
            {
                "name": "Rex",
                "events": [
                    {"type": "battery_low", "threshold": 40, "suppress_when_charging": True, "target": "phone"},
                    {"type": "collar_offline", "minutes": 60, "target": "phone", "max_per_window": 2},
                    {"type": "lost_mode", "target": "phone"},
                ],
            }
        ],
    } | kw


def test_valid() -> None:
    cfg = settings.FiSettings.model_validate(raw())
    event = cfg.collars[0].events[0]

    assert isinstance(event, settings.BatteryLow)
    assert event.threshold == 40
    assert cfg.collars[0].events[1].max_per_window == 2
    assert cfg.password.get_secret_value() == "pw"
    assert cfg.poll_interval is None
    assert [e.type for e in cfg.events()] == ["battery_low", "collar_offline", "lost_mode"]


def test_unknown_event_type() -> None:
    bad = raw(collars=[{"name": "Rex", "events": [{"type": "geofence", "target": "t"}]}])
    with pytest.raises(pydantic.ValidationError):
        settings.FiSettings.model_validate(bad)


def test_duplicate_collar_names() -> None:
    collar = {"name": "Rex", "events": []}
    with pytest.raises(ValueError, match="duplicate collar names: Rex"):
        settings.FiSettings.model_validate(raw(collars=[collar, collar]))


def test_password_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pw").write_text("secret\n")
    data = {k: v for k, v in raw().items() if k != "password"} | {"password_file": str(tmp_path / "pw")}
    assert settings.FiSettings.model_validate(data).password.get_secret_value() == "secret"


def test_password_required() -> None:
    data = {k: v for k, v in raw().items() if k != "password"}
    with pytest.raises(ValueError, match="password"):
        settings.FiSettings.model_validate(data)


def test_password_both_forms() -> None:
    with pytest.raises(ValueError, match="password"):
        settings.FiSettings.model_validate(raw(password_file="/x"))


def test_secret_hidden() -> None:
    assert "pw" not in repr(settings.FiSettings.model_validate(raw()).password)
    with pytest.raises(pydantic.ValidationError) as exc:
        settings.FiSettings.model_validate(raw(email=5, password="hunter2"))
    assert "hunter2" not in str(exc.value)


def test_poll_interval_below_60_rejected() -> None:
    with pytest.raises(ValueError, match="poll_interval"):
        settings.FiSettings.model_validate(raw(poll_interval=30))
