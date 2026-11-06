"""Runs an ASGI app with uvicorn inside courier's event loop."""

import asyncio
from collections.abc import Generator
import contextlib

from starlette import types
import uvicorn


class ServeError(Exception):
    """The HTTP server could not start, e.g. its port is in use."""


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        # courier owns SIGTERM/SIGINT through loop.add_signal_handler; uvicorn's would replace them
        yield


async def _serve_until_exit(server: _Server, port: int) -> None:
    try:
        await server.serve()
    except SystemExit as exc:  # uvicorn exits on startup failure; a SystemExit would escape the event loop
        raise ServeError(f"cannot serve on port {port}; see the uvicorn error above") from exc


async def serve(app: types.ASGIApp, host: str, port: int, stop: asyncio.Event) -> None:
    """Serves `app` until `stop` is set, then shuts the server down gracefully.

    Args:
        app: The ASGI app.
        host: Interface to bind.
        port: TCP port.
        stop: Set to shut down.

    Raises:
        ServeError: The server could not start, e.g. the port is in use.
    """
    config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False, lifespan="off")
    server = _Server(config)
    server_task = asyncio.create_task(_serve_until_exit(server, port))
    stop_requested = asyncio.create_task(stop.wait())

    await asyncio.wait({server_task, stop_requested}, return_when=asyncio.FIRST_COMPLETED)

    server.should_exit = True
    stop_requested.cancel()
    await server_task
