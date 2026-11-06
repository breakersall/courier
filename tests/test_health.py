import asyncio
from collections.abc import AsyncGenerator
import contextlib

from courier import health


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_healthy_initially() -> None:
    assert health.Health({"fi": 60}, Clock()).stale() == []


def test_unhealthy_after_3x() -> None:
    clock = Clock()
    monitor = health.Health({"fi": 60}, clock)

    clock.now += 180
    assert monitor.stale() == []

    clock.now += 1
    assert monitor.stale() == ["fi"]


def test_mark_ok_recovers() -> None:
    clock = Clock()
    monitor = health.Health({"fi": 60}, clock)
    clock.now += 500
    monitor.mark_ok("fi")
    assert monitor.stale() == []


async def get(port: int, path: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read()
    writer.close()

    return data


@contextlib.asynccontextmanager
async def serving(monitor: health.Health) -> AsyncGenerator[int]:
    server = await health.serve_health(monitor, 0, host="127.0.0.1")

    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def test_http_200_503_404() -> None:
    clock = Clock()

    async with serving(health.Health({"fi": 60}, clock)) as port:
        assert (await get(port, "/healthz")).startswith(b"HTTP/1.1 200 OK")

        clock.now += 1000
        assert (await get(port, "/readyz")).startswith(b"HTTP/1.1 503 Service Unavailable")
        assert (await get(port, "/healthz")).startswith(b"HTTP/1.1 200 OK")
        assert (await get(port, "/x")).startswith(b"HTTP/1.1 404 Not Found")


async def test_one_stale_source_is_unhealthy() -> None:
    clock = Clock()
    monitor = health.Health({"fi": 300, "commute": 120}, clock)

    clock.now += 400
    monitor.mark_ok("fi")
    assert monitor.stale() == ["commute"]

    async with serving(monitor) as port:
        data = await get(port, "/readyz")
        assert data.startswith(b"HTTP/1.1 503")
        assert b"commute" in data.split(b"\r\n\r\n", 1)[1]


async def test_metrics_endpoint() -> None:
    async with serving(health.Health({"fi": 60}, Clock())) as port:
        data = await get(port, "/metrics")
        assert data.startswith(b"HTTP/1.1 200 OK")
        assert b"# TYPE courier_polls_total counter" in data
        assert b"python_gc" not in data
