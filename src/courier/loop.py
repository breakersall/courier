"""The service: one independent poll task per source, sending notifications through rate-limited targets."""

import asyncio
from collections.abc import AsyncGenerator
import contextlib
import dataclasses
import datetime
import functools
import logging

import httpx
from starlette import applications
import uvicorn

from courier import health
from courier import limiter
from courier import message
from courier import metrics
from courier import settings
from courier import sources
from courier import state as state_store
from courier.targets import ntfy

log = logging.getLogger("courier")

type States = dict[state_store.StateKey, limiter.LimiterState]


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Waits `seconds` or until `stop` is set; replaced in tests."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def deliver(  # noqa: PLR0913, PLR0917 - each argument is separate per-poll state
    source_name: str,
    checks: list[message.Check],
    targets: dict[str, ntfy.NtfyTarget],
    http: httpx.AsyncClient,
    states: States,
    now: datetime.datetime,
) -> None:
    """Sends the matched checks the limiter allows, recording each successful send in `states`.

    Logs every decision: sent and failed at INFO and ERROR, held and unmatched at DEBUG.
    Failed sends do not stop the remaining checks. Call only after a successful poll:
    state for this source's keys that did not match is deleted, so the next match of a
    cleared event starts a fresh budget.

    Args:
        source_name: Configured source name; first half of the state key.
        checks: Checks from this poll.
        targets: Delivery targets by name.
        http: HTTP client for ntfy.
        states: Limiter state; mutated in place.
        now: Current time in the configured timezone.
    """
    still_matching = set()

    for check in checks:
        matched_message = check.message
        if matched_message is None:
            log.debug("%s/%s no match: %s", source_name, check.key, check.reason)
            continue

        key = (source_name, check.key)
        still_matching.add(key)
        event = check.event
        target = targets[event.target]
        current = states.get(key, limiter.LimiterState())

        held = limiter.blocked(current, now, target.window, event.max_per_window)
        if held is not None:
            log.debug("%s/%s matched (%s), held: %s", source_name, check.key, check.reason, held)
            metrics.NOTIFICATIONS.labels(source_name, event.target, "limited").inc()
            continue

        prioritized = dataclasses.replace(matched_message, priority=event.priority or matched_message.priority)
        try:
            await ntfy.send_ntfy(http, target, prioritized)
        except ntfy.NotifyError as exc:
            log.error("notify %s for %s/%s failed: %s", event.target, source_name, check.key, exc)
            metrics.NOTIFICATIONS.labels(source_name, event.target, "failed").inc()
            continue
        except Exception:  # a bug sending one notification must not drop the rest
            log.exception("notify %s for %s/%s crashed", event.target, source_name, check.key)
            metrics.NOTIFICATIONS.labels(source_name, event.target, "failed").inc()
            continue

        log.info("%s/%s matched (%s), sent to %s", source_name, check.key, check.reason, event.target)
        metrics.NOTIFICATIONS.labels(source_name, event.target, "sent").inc()
        states[key] = limiter.record(current, now, target.window, event.max_per_window)

    # only matched checks keep state; an unmatched one must let its budget reset
    cleared = [key for key in states if key[0] == source_name and key not in still_matching]
    for key in cleared:
        del states[key]


def _save(cfg: settings.Settings, states: States) -> None:
    try:
        state_store.save_state(cfg.state_file, states)
    except OSError as exc:  # keep running on in-memory state rather than crash-loop
        log.error("cannot write state file %s: %s", cfg.state_file, exc)


@contextlib.asynccontextmanager
async def _run_sources(
    cfg: settings.Settings,
    monitor: health.Health,
    intervals: dict[str, int],
    _app: applications.Starlette,
) -> AsyncGenerator[None]:
    """Polls every source in its own task until the context exits; the app's lifespan."""
    stop = asyncio.Event()
    states = state_store.load_state(cfg.state_file, datetime.datetime.now(cfg.timezone).date())

    async def poll_forever(name: str, source: message.Source, notify_http: httpx.AsyncClient) -> None:
        """Polls one source until `stop`; never raises, so one source cannot stop the others."""
        started = False

        while not stop.is_set():
            now = datetime.datetime.now(cfg.timezone)
            phase = "start"

            try:
                if not started:
                    await source.start()
                    started = True

                phase = "poll"
                checks = await source.poll(now)
                await deliver(name, checks, cfg.targets, notify_http, states, now)
                _save(cfg, states)
            except (message.SourceAuthError, message.SourceConfigError) as exc:
                # retrying risks account lockout, and the config will not fix itself
                log.error("%s %s failed, polling stopped until restart: %s", name, phase, exc)
                metrics.POLLS.labels(name, "error").inc()
                return
            except (message.SourceError, httpx.HTTPError, ValueError) as exc:
                # some httpx timeouts have no message
                log.error("%s %s failed: %s", name, phase, str(exc) or type(exc).__name__)
                metrics.POLLS.labels(name, "error").inc()
            except Exception:  # a bug in one source must not stop the others
                log.exception("%s %s crashed", name, phase)
                metrics.POLLS.labels(name, "error").inc()
            else:
                monitor.mark_ok(name)
                metrics.POLLS.labels(name, "ok").inc()
                metrics.LAST_SUCCESS.labels(name).set_to_current_time()

            await _wait(stop, intervals[name])

    async with contextlib.AsyncExitStack() as stack:
        notify_http = await stack.enter_async_context(httpx.AsyncClient(timeout=30))
        tasks = []

        for name, source_cfg in cfg.sources.items():
            # a client per source, so login sessions (cookies) never mix between sources
            source_http = await stack.enter_async_context(httpx.AsyncClient(timeout=30))
            source = sources.build_source(name, source_cfg, source_http, cfg.timezone)
            tasks.append(asyncio.create_task(poll_forever(name, source, notify_http)))

        try:
            yield
        finally:
            stop.set()
            await asyncio.gather(*tasks)

    log.info("stopped")


def app(cfg: settings.Settings) -> applications.Starlette:
    """Builds the service: the health endpoints, with every source polling while the app runs.

    Args:
        cfg: Service settings.

    Returns:
        The ASGI app.
    """
    intervals = {name: src.poll_interval or cfg.poll_interval for name, src in cfg.sources.items()}
    monitor = health.Health(intervals)

    return health.app(monitor, functools.partial(_run_sources, cfg, monitor, intervals))


def run(cfg: settings.Settings) -> None:
    """Serves the health endpoints and polls every source until SIGTERM or SIGINT.

    Args:
        cfg: Service settings.

    Raises:
        SystemExit: The health port could not be bound.
    """
    uvicorn.run(
        app(cfg),
        host="0.0.0.0",  # noqa: S104 - must be reachable by Kubernetes probes
        port=cfg.health_port,
        log_config=None,
        access_log=False,
        lifespan="on",  # "auto" would keep serving if polling failed to start
        timeout_graceful_shutdown=5,
    )
