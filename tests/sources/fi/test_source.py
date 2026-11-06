import datetime
import json
import pathlib
import zoneinfo

import httpx
import pytest

from courier import message
from courier.sources import fi
from courier.sources.fi import client
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


async def test_poll_returns_alert_keys() -> None:
    alerts = await source("Rex").poll(NOW)
    assert [a.key for a in alerts] == ["Rex:battery_low:phone"]
    assert alerts[0].message.title == "Rex: Battery Low"
    assert alerts[0].event.target == "phone"


async def test_poll_missing_collar_warns(caplog: pytest.LogCaptureFixture) -> None:
    assert await source("Ghost").poll(NOW) == []
    assert "Ghost" in caplog.text


def test_fi_auth_error_is_source_auth_error() -> None:
    assert issubclass(client.FiAuthError, message.SourceAuthError)
    assert issubclass(client.FiError, message.SourceError)
