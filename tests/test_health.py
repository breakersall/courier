from collections.abc import AsyncGenerator
import contextlib

import httpx
import prometheus_client

from courier import health


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_unhealthy_after_3x() -> None:
    clock = Clock()
    monitor = health.Health({"fi": 60}, clock)

    clock.now += 180
    assert monitor.stale() == []

    clock.now += 1
    assert monitor.stale() == ["fi"]


@contextlib.asynccontextmanager
async def client(monitor: health.Health) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=health.app(monitor)), base_url="http://t"
    ) as http_client:
        yield http_client


async def test_http_200_503_404() -> None:
    clock = Clock()

    async with client(health.Health({"fi": 60}, clock)) as http_client:
        assert (await http_client.get("/healthz")).status_code == 200

        clock.now += 1000
        response = await http_client.get("/readyz")
        assert (response.status_code, response.text) == (503, "503 Service Unavailable: fi")
        assert (await http_client.get("/healthz")).text == "200 OK"
        assert (await http_client.get("/x")).status_code == 404


async def test_one_stale_source_is_unhealthy() -> None:
    clock = Clock()
    monitor = health.Health({"fi": 300, "commute": 120}, clock)

    clock.now += 400
    monitor.mark_ok("fi")
    assert monitor.stale() == ["commute"]

    async with client(monitor) as http_client:
        response = await http_client.get("/readyz")
        assert response.status_code == 503
        assert response.text.endswith("commute")


async def test_metrics_endpoint() -> None:
    async with client(health.Health({"fi": 60}, Clock())) as http_client:
        response = await http_client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"] == prometheus_client.CONTENT_TYPE_LATEST
        assert "# TYPE courier_polls_total counter" in response.text
        assert "python_gc" not in response.text
