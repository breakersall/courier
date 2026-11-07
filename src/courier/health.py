"""Health tracking and the /healthz, /readyz, and /metrics endpoints."""

from collections.abc import Callable
import time

import prometheus_client
from starlette import applications
from starlette import requests
from starlette import responses
from starlette import routing
from starlette import types

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
        stale = []
        for name, limit in self._limits.items():
            if now - self._last_ok[name] > limit:
                stale.append(name)

        return sorted(stale)


def app(health: Health, lifespan: types.Lifespan[applications.Starlette] | None = None) -> applications.Starlette:
    """Builds the app serving /healthz, /readyz, and /metrics.

    /healthz returns 200 while the event loop serves requests. /readyz returns 200, or 503
    naming the stale sources. /metrics returns Prometheus metrics. Any other path returns 404.

    Args:
        health: Health tracker to report.
        lifespan: Runs while the app serves; the service polls its sources in it.

    Returns:
        The ASGI app.
    """

    async def healthz(request: requests.Request) -> responses.Response:
        return responses.PlainTextResponse("200 OK")

    async def readyz(request: requests.Request) -> responses.Response:
        stale = health.stale()

        if not stale:
            return responses.PlainTextResponse("200 OK")

        return responses.PlainTextResponse(f"503 Service Unavailable: {', '.join(stale)}", status_code=503)

    async def prometheus_metrics(request: requests.Request) -> responses.Response:
        return responses.Response(
            prometheus_client.generate_latest(metrics.REGISTRY), media_type=prometheus_client.CONTENT_TYPE_LATEST
        )

    return applications.Starlette(
        lifespan=lifespan,
        routes=[
            routing.Route("/healthz", healthz),
            routing.Route("/readyz", readyz),
            routing.Route("/metrics", prometheus_metrics),
        ],
    )
