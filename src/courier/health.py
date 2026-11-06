"""Minimal /healthz, /readyz, and /metrics endpoints on stdlib asyncio."""

import asyncio
from collections.abc import Callable
import contextlib
import time

import prometheus_client

from courier import metrics


class Health:
    """Tracks whether each source's polling is keeping up.

    Args:
        intervals: Poll interval in seconds per source name; a source is stale after 3x without success.
        clock: Monotonic clock, injectable for tests.
    """

    def __init__(self, intervals: dict[str, int], clock: Callable[[], float] = time.monotonic) -> None:
        self._limits = {name: 3 * interval for name, interval in intervals.items()}
        self._clock = clock
        self._last_ok = dict.fromkeys(intervals, clock())

    def mark_ok(self, name: str) -> None:
        """Records a successful poll of source `name`."""
        self._last_ok[name] = self._clock()

    def stale(self) -> list[str]:
        """Returns sorted names of sources with no success within 3x their interval."""
        now = self._clock()
        return sorted(name for name, limit in self._limits.items() if now - self._last_ok[name] > limit)


async def serve_health(
    health: Health,
    port: int,
    host: str = "0.0.0.0",  # noqa: S104 - must be reachable by Kubernetes probes
) -> asyncio.Server:
    """Starts the HTTP server for /healthz, /readyz, and /metrics.

    /healthz returns 200 while the event loop serves requests. /readyz returns 200, or 503
    naming the stale sources. /metrics returns Prometheus metrics. Any other path returns 404.

    Args:
        health: Health tracker to report.
        port: TCP port; 0 picks a free one.
        host: Interface to bind.

    Returns:
        The running server; the caller closes it.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            parts = (await asyncio.wait_for(reader.readline(), 5)).split()
            path = parts[1] if len(parts) >= 2 else b""
            stale = health.stale()
            content_type = "text/plain"

            if path == b"/metrics":
                status = "200 OK"
                body = metrics.render()
                content_type = prometheus_client.CONTENT_TYPE_LATEST
            elif path not in {b"/healthz", b"/readyz"}:
                status = "404 Not Found"
                body = status.encode()
            elif path == b"/healthz" or not stale:
                status = "200 OK"
                body = status.encode()
            else:
                status = "503 Service Unavailable"
                body = f"{status}: {', '.join(stale)}".encode()

            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except TimeoutError, ConnectionError, ValueError:  # ValueError: request line over the stream limit
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    return await asyncio.start_server(handle, host, port)
