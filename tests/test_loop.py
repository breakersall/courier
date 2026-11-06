import asyncio
import datetime
import functools
import json
import pathlib
from typing import Any
import zoneinfo

import httpx
import pytest

from courier import loop
from courier import message
from courier import metrics
from courier import settings
from courier import sources
from courier.sources.fi import settings as fi_settings
from courier.targets import ntfy

NY = zoneinfo.ZoneInfo("America/New_York")
MSG = message.Message("Rex: Battery Low", "body", "default", "battery")


def make_settings(tmp_path: pathlib.Path, intervals: dict[str, int] | None = None) -> settings.Settings:
    intervals = intervals or {"dogs": 60}
    cfg = settings.Settings.model_validate(
        {
            "poll_interval": 300,
            "timezone": "America/New_York",
            "state_file": str(tmp_path / "state.json"),
            "targets": {"phone": {"type": "ntfy", "url": "https://ntfy.example.com/x", "token": "t"}},
            "sources": {
                name: {
                    "type": "fi",
                    "email": "e",
                    "password": "p",
                    "poll_interval": interval,
                    "collars": [{"name": "Rex", "events": [{"type": "battery_low", "target": "phone"}]}],
                }
                for name, interval in intervals.items()
            },
        }
    )

    return cfg.model_copy(update={"health_port": 0})  # free port; the field itself requires >= 1


def at(hh: int, mm: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 10, 9, hh, mm, tzinfo=NY)


def target(window: dict[str, str] | None = None) -> dict[str, ntfy.NtfyTarget]:
    return {
        "phone": ntfy.NtfyTarget.model_validate(
            {"type": "ntfy", "url": "https://ntfy.example.com/x", "token": "t", "window": window}
        )
    }


def alert(limit: int | None = 2, priority: str | None = None, key: str = "Rex:battery_low:phone") -> message.Alert:
    event = fi_settings.BatteryLow.model_validate(
        {"type": "battery_low", "target": "phone", "max_per_window": limit, "priority": priority}
    )
    return message.Alert(key, event, MSG)


class Ntfy:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def deliver_at(ntfy_: Ntfy, alerts: list[message.Alert], *times: datetime.datetime, **window: str) -> loop.States:
    states: loop.States = {}
    async with ntfy_.client() as http:
        for t in times:
            await loop.deliver("dogs", alerts, target(window or None), http, states, t)
    return states


async def test_deliver_respects_limit() -> None:
    ntfy_ = Ntfy()
    states = await deliver_at(ntfy_, [alert()], at(8), at(8, 5), at(14), start="08:00", end="20:00")
    assert len(ntfy_.requests) == 2
    assert states[("dogs", "Rex:battery_low:phone")].sent == 2


def notifications(result: str) -> float:
    return (
        metrics.REGISTRY.get_sample_value(
            "courier_notifications_total", {"source": "dogs", "target": "phone", "result": result}
        )
        or 0.0
    )


async def test_deliver_counts_metrics() -> None:
    sent, limited = notifications("sent"), notifications("limited")
    await deliver_at(Ntfy(), [alert(limit=1)], at(8), at(8, 5), start="08:00", end="20:00")

    assert notifications("sent") - sent == 1
    assert notifications("limited") - limited == 1


async def test_deliver_outside_window() -> None:
    ntfy_ = Ntfy()
    states = await deliver_at(ntfy_, [alert()], at(21), start="08:00", end="20:00")
    assert ntfy_.requests == []
    assert states == {}


async def test_deliver_failed_send_not_recorded(caplog: pytest.LogCaptureFixture) -> None:
    ntfy_ = Ntfy(status=500)
    async with ntfy_.client() as http:
        states: loop.States = {}
        sent = await loop.deliver("dogs", [alert()], target(), http, states, at(9))

    assert not sent
    assert len(ntfy_.requests) == 1
    assert states == {}
    assert "HTTP 500" in caplog.text


async def test_deliver_priority_override() -> None:
    ntfy_ = Ntfy()
    await deliver_at(ntfy_, [alert(priority="urgent")], at(9))
    assert ntfy_.requests[0].url.params["priority"] == "urgent"


async def test_deliver_unexpected_failure_does_not_skip_rest(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = ntfy.send_ntfy
    calls = 0

    async def flaky(http: httpx.AsyncClient, tgt: ntfy.NtfyTarget, msg: message.Message) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        await real(http, tgt, msg)

    monkeypatch.setattr(ntfy, "send_ntfy", flaky)
    ntfy_ = Ntfy()
    states = await deliver_at(ntfy_, [alert(key="a"), alert(key="b")], at(9))
    assert len(ntfy_.requests) == 1
    assert set(states) == {("dogs", "b")}
    assert "boom" in caplog.text


class FakeSource:
    def __init__(self, results: list[Any] | None = None, start_errors: list[BaseException] | None = None) -> None:
        self.results = results or []
        self.start_errors = start_errors or []
        self.polls = 0

    async def start(self) -> None:
        if self.start_errors:
            raise self.start_errors.pop(0)

    async def poll(self, now: datetime.datetime) -> list[message.Alert]:
        self.polls += 1
        result = self.results.pop(0) if self.results else []
        if isinstance(result, BaseException):
            raise result
        return result


class VirtualClock:
    """Fake `wait`: each poll task advances its own virtual time; stop is set once every task reaches `until`."""

    def __init__(self, tasks: int, until: float) -> None:
        self.tasks = tasks
        self.until = until
        self.elapsed: dict[asyncio.Task[Any] | None, float] = {}

    async def __call__(self, stop: asyncio.Event, seconds: float) -> None:
        task = asyncio.current_task()
        self.elapsed[task] = self.elapsed.get(task, 0) + seconds

        if self.elapsed[task] < self.until:
            await asyncio.sleep(0)
            return

        if sum(t >= self.until for t in self.elapsed.values()) == self.tasks:
            stop.set()
        await stop.wait()


@pytest.fixture
def ntfy_mock(monkeypatch: pytest.MonkeyPatch) -> Ntfy:
    fake = Ntfy()
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(fake.handler))
    )
    return fake


def patch_sources(monkeypatch: pytest.MonkeyPatch, fakes: dict[str, FakeSource]) -> dict[str, httpx.AsyncClient]:
    clients: dict[str, httpx.AsyncClient] = {}

    def build(name: str, cfg: sources.SourceConfig, http: httpx.AsyncClient, tz: zoneinfo.ZoneInfo) -> FakeSource:
        clients[name] = http
        return fakes[name]

    monkeypatch.setattr(sources, "build_source", build)
    return clients


async def test_two_sources_independent_intervals(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy
) -> None:
    fakes = {"fast": FakeSource(), "slow": FakeSource()}
    patch_sources(monkeypatch, fakes)
    await loop.run(make_settings(tmp_path, {"fast": 60, "slow": 120}), wait=VirtualClock(2, 600))
    assert (fakes["fast"].polls, fakes["slow"].polls) == (10, 5)


async def test_sources_get_separate_http_clients(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy
) -> None:
    clients = patch_sources(monkeypatch, {"a": FakeSource(), "b": FakeSource()})
    await loop.run(make_settings(tmp_path, {"a": 60, "b": 60}), wait=VirtualClock(2, 60))
    assert clients["a"] is not clients["b"]


async def test_poll_auth_error_stops_only_that_source(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy, caplog: pytest.LogCaptureFixture
) -> None:
    fakes = {"bad": FakeSource([message.SourceAuthError("rejected")]), "good": FakeSource()}
    patch_sources(monkeypatch, fakes)
    await loop.run(make_settings(tmp_path, {"bad": 60, "good": 60}), wait=VirtualClock(1, 600))

    assert (fakes["bad"].polls, fakes["good"].polls) == (1, 10)
    assert "bad credentials rejected" in caplog.text


async def test_start_auth_error_exits(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy) -> None:
    patch_sources(monkeypatch, {"dogs": FakeSource(start_errors=[message.SourceAuthError("bad login")])})
    with pytest.raises(SystemExit, match=r"^dogs: start failed: bad login$"):
        await loop.run(make_settings(tmp_path), wait=VirtualClock(1, 600))


async def test_start_config_error_exits(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy
) -> None:
    patch_sources(monkeypatch, {"dogs": FakeSource(start_errors=[message.SourceConfigError("collars not found")])})
    with pytest.raises(SystemExit, match=r"^dogs: start failed: collars not found$"):
        await loop.run(make_settings(tmp_path), wait=VirtualClock(1, 600))


async def test_start_error_retries_while_others_run(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy, caplog: pytest.LogCaptureFixture
) -> None:
    fakes = {"down": FakeSource(start_errors=[httpx.ConnectError("down")] * 3), "up": FakeSource()}
    patch_sources(monkeypatch, fakes)
    await loop.run(make_settings(tmp_path, {"down": 60, "up": 60}), wait=VirtualClock(2, 600))

    assert (fakes["down"].polls, fakes["up"].polls) == (7, 10)
    assert "down start failed: down" in caplog.text


async def test_source_error_keeps_running(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy, caplog: pytest.LogCaptureFixture
) -> None:
    patch_sources(monkeypatch, {"dogs": FakeSource([message.SourceError("upstream down"), [alert()]])})
    await loop.run(make_settings(tmp_path), wait=VirtualClock(1, 120))
    assert "upstream down" in caplog.text
    assert len(ntfy_mock.requests) == 1


async def test_state_saved_and_pruned(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy) -> None:
    patch_sources(monkeypatch, {"dogs": FakeSource([[alert()]])})
    cfg = make_settings(tmp_path)

    old = {json.dumps(["dogs", "old"]): {"window_date": "2020-01-01", "sent": 1, "next_allowed": None}}
    (tmp_path / "state.json").write_text(json.dumps(old))

    await loop.run(cfg, wait=VirtualClock(1, 60))

    saved = json.loads((tmp_path / "state.json").read_text())
    today = datetime.datetime.now(cfg.timezone).date().isoformat()
    assert list(saved) == [json.dumps(["dogs", "Rex:battery_low:phone"])]
    assert saved[json.dumps(["dogs", "Rex:battery_low:phone"])]["window_date"] == today
