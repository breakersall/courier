import collections
import dataclasses
import datetime
import pathlib

from google.transit import gtfs_realtime_pb2
import httpx
import pytest

from courier import message
from courier.sources import metra
from courier.sources.metra import gtfs
from courier.sources.metra import settings

FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures"
NOW = datetime.datetime(2026, 11, 5, 5, 56, tzinfo=gtfs.METRA_TZ)
ALERT_ID = "DevAPI-4CB43442-B843-4E77-A336-EFDACE89B1CE"


class Env:
    """Mock Metra server plus a controllable clock."""

    def __init__(self, zip_bytes: bytes) -> None:
        self.zip = zip_bytes
        self.published = "v1"
        self.zip_status = 200
        self.counts: collections.Counter[str] = collections.Counter()
        self.now = 0.0
        self.alerts: bytes | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        self.counts[name] += 1

        if name == "published.txt":
            return httpx.Response(200, text=self.published)
        if name == "schedule.zip":
            return httpx.Response(self.zip_status, content=self.zip)
        if name == "alerts" and self.alerts is not None:
            return httpx.Response(200, content=self.alerts)
        return httpx.Response(200, content=(FIXTURES / f"metra_{name}.pb").read_bytes())

    def source(self, trains: list[dict] | None = None, lines: list[dict] | None = None) -> metra.MetraSource:
        cfg = settings.MetraSettings.model_validate(
            {"type": "metra", "token": "t", "trains": trains or [], "lines": lines or []}
        )
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return metra.MetraSource(cfg, http, gtfs.METRA_TZ, clock=lambda: self.now)


def train(line: str, number: str) -> dict:
    return {"line": line, "number": number, "events": [{"type": "train_delayed", "target": "phone", "minutes": 1}]}


def line(line_id: str) -> dict:
    return {"id": line_id, "events": [{"type": "service_alert", "target": "phone"}]}


@pytest.fixture
def env(bn1204_zip) -> Env:
    return Env(bn1204_zip)


async def test_start_missing_train_lists_available(env) -> None:
    with pytest.raises(SystemExit, match=r"^metra: .*BNSF 9999.*1204"):
        await env.source(trains=[train("BNSF", "9999")]).start()


async def test_start_missing_line(env) -> None:
    with pytest.raises(SystemExit, match=r"^metra: .*XX.*BNSF"):
        await env.source(lines=[line("XX")]).start()


async def test_start_schedule_error_raises(env) -> None:
    env.zip_status = 500
    with pytest.raises(message.SourceError, match="HTTP 500"):
        await env.source(trains=[train("BNSF", "1204")]).start()


async def test_poll_delay_alert_key(env) -> None:
    src = env.source(trains=[train("BNSF", "1204")])
    await src.start()
    alerts = await src.poll(NOW)
    assert [a.key for a in alerts] == ["BNSF1204:train_delayed:phone"]


async def test_poll_alert_keys_include_alert_id(env) -> None:
    src = env.source(lines=[line("HC")])
    await src.start()
    alerts = await src.poll(NOW)
    assert alerts
    assert alerts[0].key == f"HC:service_alert:phone:{ALERT_ID}"


async def test_poll_skips_unneeded_feeds(env) -> None:
    src = env.source(lines=[line("HC")])
    await src.start()
    await src.poll(NOW)
    assert env.counts["tripupdates"] == 0
    assert env.counts["alerts"] == 1


async def test_schedule_refresh_only_when_published_changes(env) -> None:
    src = env.source(trains=[train("BNSF", "1204")])
    await src.start()

    env.now = 3601
    await src.poll(NOW)
    assert env.counts["schedule.zip"] == 1

    env.published = "v2"
    env.now = 7202
    await src.poll(NOW)
    assert env.counts["schedule.zip"] == 2


async def test_schedule_refresh_failure_keeps_old(env) -> None:
    src = env.source(trains=[train("BNSF", "1204")])
    await src.start()

    env.published = "v2"
    env.zip_status = 500
    env.now = 3601
    alerts = await src.poll(NOW)

    assert [a.key for a in alerts] == ["BNSF1204:train_delayed:phone"]


async def test_poll_alert_on_two_lines_emitted_once(env) -> None:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString((FIXTURES / "metra_alerts.pb").read_bytes())
    for e in feed.entity:
        if e.id == ALERT_ID:
            e.alert.informed_entity.add(route_id="BNSF")

    env.alerts = feed.SerializeToString()
    src = env.source(lines=[line("HC"), line("BNSF")])
    await src.start()
    keys = [a.key for a in await src.poll(NOW) if a.key.endswith(ALERT_ID)]

    assert keys == [f"HC:service_alert:phone:{ALERT_ID}"]


async def test_refresh_warns_for_train_missing_after_reload(env, caplog, monkeypatch) -> None:
    src = env.source(trains=[train("BNSF", "1204")])
    await src.start()

    real = gtfs.load_schedule
    monkeypatch.setattr(gtfs, "load_schedule", lambda *a: dataclasses.replace(real(*a), trips={}))  # renumbered
    env.published = "v2"
    env.now = 3601

    with caplog.at_level("WARNING"):
        await src.poll(NOW)

    assert "BNSF 1204" in caplog.text
