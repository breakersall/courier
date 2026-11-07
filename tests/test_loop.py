import asyncio
import datetime
import functools
import json
import os
import pathlib
import signal
import socket
from typing import Any
import zoneinfo

import httpx
import pytest

from courier import limiter
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


def check(
    limit: int | None = 2,
    priority: str | None = None,
    key: str = "Rex:battery_low:phone",
    matched: message.Message | None = MSG,
) -> message.Check:
    raw = {"type": "battery_low", "target": "phone", "priority": priority}
    if limit is not None:
        raw["max_per_window"] = limit
    event = fi_settings.BatteryLow.model_validate(raw)
    return message.Check(key, event, matched, "battery 37% < threshold 40%")


class Ntfy:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def deliver_at(ntfy_: Ntfy, polled: list[message.Check], *times: datetime.datetime, **window: str) -> loop.States:
    states: loop.States = {}
    async with ntfy_.client() as http:
        for t in times:
            await loop.deliver("dogs", polled, target(window or None), http, states, t)
    return states


async def test_deliver_respects_limit() -> None:
    ntfy_ = Ntfy()
    states = await deliver_at(ntfy_, [check()], at(8), at(8, 5), at(14), start="08:00", end="20:00")
    assert len(ntfy_.requests) == 2
    assert states[("dogs", "Rex:battery_low:phone")].sent == 2


async def test_deliver_default_once_per_window() -> None:
    ntfy_ = Ntfy()
    await deliver_at(ntfy_, [check(limit=None)], at(8), at(12), at(19))
    assert len(ntfy_.requests) == 1


async def test_deliver_cleared_alert_starts_fresh_budget() -> None:
    ntfy_ = Ntfy()
    states: loop.States = {("other", "Rex:battery_low:phone"): limiter.LimiterState()}

    async with ntfy_.client() as http:
        for polled in ([check(limit=1)], [check(limit=1)], [], [check(limit=1)]):
            await loop.deliver("dogs", polled, target(), http, states, at(9))

    assert len(ntfy_.requests) == 2
    assert ("other", "Rex:battery_low:phone") in states  # other sources' state untouched


async def test_deliver_logs_each_decision(caplog: pytest.LogCaptureFixture) -> None:
    windowed = target({"start": "08:00", "end": "20:00"})
    states: loop.States = {}

    with caplog.at_level("DEBUG", logger="courier"):
        async with Ntfy().client() as http:
            await loop.deliver("dogs", [check(key="low")], windowed, http, states, at(9))
            await loop.deliver("dogs", [check(key="low")], windowed, http, states, at(10))
            await loop.deliver("dogs", [check(key="night")], windowed, http, {}, at(21))
            await loop.deliver("dogs", [check(key="ok", matched=None)], windowed, http, {}, at(9))

    assert caplog.messages == [
        "dogs/low matched (battery 37% < threshold 40%), sent to phone",
        "dogs/low matched (battery 37% < threshold 40%), held: next allowed 14:30",
        "dogs/night matched (battery 37% < threshold 40%), held: outside window 08:00–20:00",  # noqa: RUF001
        "dogs/ok no match: battery 37% < threshold 40%",
    ]


async def test_deliver_unmatched_check_clears_state() -> None:
    key = "Rex:battery_low:phone"
    states: loop.States = {("dogs", key): limiter.LimiterState(window_date=at(9).date(), sent=1)}

    async with Ntfy().client() as http:
        changed = await loop.deliver("dogs", [check(matched=None)], target(), http, states, at(9))

    assert states == {}
    assert changed


def notifications(result: str) -> float:
    return (
        metrics.REGISTRY.get_sample_value(
            "courier_notifications_total", {"source": "dogs", "target": "phone", "result": result}
        )
        or 0.0
    )


async def test_deliver_counts_metrics() -> None:
    sent, limited = notifications("sent"), notifications("limited")
    await deliver_at(Ntfy(), [check(limit=1)], at(8), at(8, 5), start="08:00", end="20:00")

    assert notifications("sent") - sent == 1
    assert notifications("limited") - limited == 1


async def test_deliver_outside_window() -> None:
    ntfy_ = Ntfy()
    states = await deliver_at(ntfy_, [check()], at(21), start="08:00", end="20:00")
    assert ntfy_.requests == []
    assert states == {}


async def test_deliver_failed_send_not_recorded(caplog: pytest.LogCaptureFixture) -> None:
    ntfy_ = Ntfy(status=500)
    async with ntfy_.client() as http:
        states: loop.States = {}
        sent = await loop.deliver("dogs", [check()], target(), http, states, at(9))

    assert not sent
    assert len(ntfy_.requests) == 1
    assert states == {}
    assert "HTTP 500" in caplog.text


async def test_deliver_priority_override() -> None:
    ntfy_ = Ntfy()
    await deliver_at(ntfy_, [check(priority="urgent")], at(9))
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
    states = await deliver_at(ntfy_, [check(key="a"), check(key="b")], at(9))
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

    async def poll(self, now: datetime.datetime) -> list[message.Check]:
        self.polls += 1
        if not self.results:
            return []

        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class VirtualClock:
    """Fake `wait`: each poll task advances its own virtual time; sets `reached` once `tasks` of them reach `until`."""

    def __init__(self, tasks: int, until: float) -> None:
        self.tasks = tasks
        self.until = until
        self.elapsed: dict[asyncio.Task[Any] | None, float] = {}
        self.reached = asyncio.Event()

    async def __call__(self, stop: asyncio.Event, seconds: float) -> None:
        task = asyncio.current_task()
        self.elapsed[task] = self.elapsed.get(task, 0) + seconds

        if self.elapsed[task] < self.until:
            await asyncio.sleep(0)
            return

        if sum(t >= self.until for t in self.elapsed.values()) == self.tasks:
            self.reached.set()
        await stop.wait()


async def serve_polls(cfg: settings.Settings, clock: VirtualClock) -> None:
    """Runs the app's lifespan, as uvicorn does, until the clock is reached."""
    service = loop.app(cfg, wait=clock)

    async with asyncio.timeout(5), service.router.lifespan_context(service):
        await clock.reached.wait()


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
    await serve_polls(make_settings(tmp_path, {"fast": 60, "slow": 120}), VirtualClock(2, 600))
    assert (fakes["fast"].polls, fakes["slow"].polls) == (10, 5)


async def test_sources_get_separate_http_clients(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy
) -> None:
    clients = patch_sources(monkeypatch, {"a": FakeSource(), "b": FakeSource()})
    await serve_polls(make_settings(tmp_path, {"a": 60, "b": 60}), VirtualClock(2, 60))
    assert clients["a"] is not clients["b"]


async def test_poll_auth_error_stops_only_that_source(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy, caplog: pytest.LogCaptureFixture
) -> None:
    fakes = {"bad": FakeSource([message.SourceAuthError("rejected")]), "good": FakeSource()}
    patch_sources(monkeypatch, fakes)
    await serve_polls(make_settings(tmp_path, {"bad": 60, "good": 60}), VirtualClock(1, 600))

    assert (fakes["bad"].polls, fakes["good"].polls) == (1, 10)
    assert "bad poll failed, polling stopped until restart: rejected" in caplog.text


@pytest.mark.parametrize(
    "error", [message.SourceAuthError("bad login"), message.SourceConfigError("collars not found")], ids=str
)
async def test_start_rejected_stops_only_that_source(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    ntfy_mock: Ntfy,
    caplog: pytest.LogCaptureFixture,
    error: message.SourceError,
) -> None:
    fakes = {"bad": FakeSource(start_errors=[error]), "good": FakeSource()}
    patch_sources(monkeypatch, fakes)
    await serve_polls(make_settings(tmp_path, {"bad": 60, "good": 60}), VirtualClock(1, 600))

    assert (fakes["bad"].polls, fakes["good"].polls) == (0, 10)
    assert f"bad start failed, polling stopped until restart: {error}" in caplog.text


async def test_unexpected_error_keeps_running(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy, caplog: pytest.LogCaptureFixture
) -> None:
    patch_sources(monkeypatch, {"dogs": FakeSource([RuntimeError("bug"), [check()]])})
    await serve_polls(make_settings(tmp_path), VirtualClock(1, 120))

    assert "dogs poll crashed" in caplog.text
    assert "RuntimeError: bug" in caplog.text
    assert len(ntfy_mock.requests) == 1


def test_health_port_in_use_exits(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy) -> None:
    patch_sources(monkeypatch, {"dogs": FakeSource()})

    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 0))  # noqa: S104 - same interface the health server binds
        occupied.listen()
        port = occupied.getsockname()[1]
        cfg = make_settings(tmp_path).model_copy(update={"health_port": port})

        with pytest.raises(SystemExit):
            loop.run(cfg)


async def test_start_error_retries_while_others_run(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy, caplog: pytest.LogCaptureFixture
) -> None:
    fakes = {"down": FakeSource(start_errors=[httpx.ConnectError("down")] * 3), "up": FakeSource()}
    patch_sources(monkeypatch, fakes)
    await serve_polls(make_settings(tmp_path, {"down": 60, "up": 60}), VirtualClock(2, 600))

    assert (fakes["down"].polls, fakes["up"].polls) == (7, 10)
    assert "down start failed: down" in caplog.text


async def test_source_error_keeps_running(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy, caplog: pytest.LogCaptureFixture
) -> None:
    patch_sources(monkeypatch, {"dogs": FakeSource([message.SourceError("upstream down"), [check()]])})
    await serve_polls(make_settings(tmp_path), VirtualClock(1, 120))
    assert "upstream down" in caplog.text
    assert len(ntfy_mock.requests) == 1


async def test_failed_poll_does_not_clear(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy
) -> None:
    results = [[check(limit=1)], message.SourceError("down"), [check(limit=1)]]
    patch_sources(monkeypatch, {"dogs": FakeSource(results)})
    await serve_polls(make_settings(tmp_path), VirtualClock(1, 180))
    assert len(ntfy_mock.requests) == 1


async def test_cleared_poll_notifies_again(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy
) -> None:
    patch_sources(monkeypatch, {"dogs": FakeSource([[check(limit=1)], [], [check(limit=1)]])})
    await serve_polls(make_settings(tmp_path), VirtualClock(1, 180))
    assert len(ntfy_mock.requests) == 2


async def test_state_saved_and_pruned(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy) -> None:
    patch_sources(monkeypatch, {"dogs": FakeSource([[check()]])})
    cfg = make_settings(tmp_path)

    old = {json.dumps(["dogs", "old"]): {"window_date": "2020-01-01", "sent": 1, "next_allowed": None}}
    (tmp_path / "state.json").write_text(json.dumps(old))

    await serve_polls(cfg, VirtualClock(1, 60))

    saved = json.loads((tmp_path / "state.json").read_text())
    today = datetime.datetime.now(cfg.timezone).date().isoformat()
    assert list(saved) == [json.dumps(["dogs", "Rex:battery_low:phone"])]
    assert saved[json.dumps(["dogs", "Rex:battery_low:phone"])]["window_date"] == today


class SigtermSource(FakeSource):
    async def poll(self, now: datetime.datetime) -> list[message.Check]:
        os.kill(os.getpid(), signal.SIGTERM)
        return await super().poll(now)


def test_sigterm_stops_run(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy) -> None:
    patch_sources(monkeypatch, {"dogs": SigtermSource()})
    received = []

    def record(signum: int, frame: object) -> None:
        received.append(signum)

    # uvicorn raises the signal again once it has shut down; record it instead of ending pytest
    previous = signal.signal(signal.SIGTERM, record)
    try:
        loop.run(make_settings(tmp_path))
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert received == [signal.SIGTERM]


async def test_error_without_message_names_the_exception(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, ntfy_mock: Ntfy, caplog: pytest.LogCaptureFixture
) -> None:
    patch_sources(monkeypatch, {"dogs": FakeSource([httpx.ReadTimeout("")])})
    await serve_polls(make_settings(tmp_path), VirtualClock(1, 60))

    assert "dogs poll failed: ReadTimeout" in caplog.text
