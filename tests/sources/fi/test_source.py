import datetime
import json
import pathlib
import zoneinfo

import httpx
import pytest

from courier import message
from courier.sources import fi
from courier.sources.fi import settings

FIXTURE = json.loads((pathlib.Path(__file__).parents[2] / "fixtures" / "households.json").read_text())
NOW = datetime.datetime(2026, 10, 9, 15, 0, tzinfo=datetime.UTC)
NY = zoneinfo.ZoneInfo("America/New_York")


def source(*names: str) -> fi.FiSource:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"userId": "u"})
        return httpx.Response(200, json=FIXTURE)

    cfg = settings.FiSettings.model_validate(
        {
            "type": "fi",
            "email": "e",
            "password": "p",
            "collars": [{"name": n, "events": [{"type": "battery_low", "target": "phone"}]} for n in names],
        }
    )
    return fi.FiSource(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)), NY)


async def test_start_ok() -> None:
    await source("Rex").start()


async def test_start_missing_collar_raises() -> None:
    with pytest.raises(message.SourceConfigError, match=r"Ghost.*available: Bella, Broken, Rex"):
        await source("Ghost").start()


async def test_poll_returns_checks() -> None:
    (battery_check,) = await source("Rex").poll(NOW)

    assert battery_check.key == "Rex:battery_low:phone"
    assert battery_check.message is not None
    assert battery_check.message.title == "Rex: Battery Low"
    assert battery_check.event.target == "phone"
    assert battery_check.reason.startswith("battery ")


async def test_poll_unmatched_check_has_reason() -> None:
    (battery_check,) = await source("Bella").poll(NOW)

    assert battery_check.message is None
    assert "≥ threshold" in battery_check.reason


async def test_poll_missing_collar_has_unmatched_checks(caplog: pytest.LogCaptureFixture) -> None:
    (missing,) = await source("Ghost").poll(NOW)

    assert missing.message is None
    assert missing.reason == "missing from Fi response"
    assert "Ghost" in caplog.text
