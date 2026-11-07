import asyncio
from collections.abc import AsyncGenerator
import contextlib
import os
import signal
import socket

import httpx
import pytest

from courier import health
from courier import web


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def get(port: int, path: str) -> httpx.Response:
    async with httpx.AsyncClient() as http_client:
        for _ in range(50):  # the server binds shortly after its task starts
            try:
                return await http_client.get(f"http://127.0.0.1:{port}{path}")
            except httpx.ConnectError:
                await asyncio.sleep(0.05)
    raise AssertionError(f"nothing listening on {port}")


@contextlib.asynccontextmanager
async def serving(port: int) -> AsyncGenerator[asyncio.Event]:
    stop = asyncio.Event()
    server_task = asyncio.create_task(web.serve(health.app(health.Health({})), "127.0.0.1", port, stop))

    try:
        yield stop
    finally:
        stop.set()
        await asyncio.wait_for(server_task, 5)


async def test_serve_until_stop() -> None:
    port = free_port()

    async with serving(port):
        assert (await get(port, "/healthz")).status_code == 200


async def test_serve_keeps_loop_signal_handlers() -> None:
    loop = asyncio.get_running_loop()
    received = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, received.set)
    handler_before_serve = signal.getsignal(signal.SIGTERM)
    port = free_port()

    try:
        async with serving(port):
            await get(port, "/healthz")
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(received.wait(), 5)
            assert (await get(port, "/healthz")).status_code == 200

        assert signal.getsignal(signal.SIGTERM) is handler_before_serve
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


async def test_serve_port_in_use_exits() -> None:
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]

        with pytest.raises(web.ServeError, match=f"port {port}"):
            await asyncio.wait_for(web.serve(health.app(health.Health({})), "127.0.0.1", port, asyncio.Event()), 5)


async def test_cancelled_serve_stops_its_server() -> None:
    healthy_port = free_port()

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        busy_port = occupied.getsockname()[1]

        async def busy_after_healthy_is_up() -> None:
            await get(healthy_port, "/healthz")
            await web.serve(health.app(health.Health({})), "127.0.0.1", busy_port, asyncio.Event())

        async def serve_both() -> None:
            async with asyncio.TaskGroup() as task_group:
                healthy = web.serve(health.app(health.Health({})), "127.0.0.1", healthy_port, asyncio.Event())
                task_group.create_task(healthy)
                task_group.create_task(busy_after_healthy_is_up())

        with pytest.raises(ExceptionGroup):
            await serve_both()

    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as http_client:
            await http_client.get(f"http://127.0.0.1:{healthy_port}/healthz")
