"""Poll loop: one task per source, delivering alerts through rate-limited targets."""

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import dataclasses
import datetime
import logging
import signal

import httpx

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
type Wait = Callable[[asyncio.Event, float], Awaitable[None]]


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def deliver(  # noqa: PLR0913, PLR0917 - each argument is separate per-poll state
    source_name: str,
    alerts: list[message.Alert],
    targets: dict[str, ntfy.NtfyTarget],
    http: httpx.AsyncClient,
    states: States,
    now: datetime.datetime,
) -> bool:
    """Sends the alerts the limiter allows, recording each successful send in `states`.

    Failed sends are logged, not raised, and do not stop the remaining alerts.

    Args:
        source_name: Configured source name; first half of the state key.
        alerts: Alerts from this poll.
        targets: Delivery targets by name.
        http: HTTP client for ntfy.
        states: Limiter state; mutated in place.
        now: Current time in the configured timezone.

    Returns:
        Whether anything was sent.
    """
    sent = False

    for alert in alerts:
        event = alert.event
        target = targets[event.target]
        key = (source_name, alert.key)
        current = states.get(key, limiter.LimiterState())

        if not limiter.allowed(current, now, target.window, event.max_per_window):
            log.debug("%s/%s matched but limited", source_name, alert.key)
            metrics.NOTIFICATIONS.labels(source_name, event.target, "limited").inc()
            continue

        msg = dataclasses.replace(alert.message, priority=event.priority or alert.message.priority)
        try:
            await ntfy.send_ntfy(http, target, msg)
        except ntfy.NotifyError as exc:
            log.error("notify %s for %s/%s failed: %s", event.target, source_name, alert.key, exc)
            metrics.NOTIFICATIONS.labels(source_name, event.target, "failed").inc()
            continue
        except Exception:  # one buggy alert must not drop the rest
            log.exception("notify %s for %s/%s crashed", event.target, source_name, alert.key)
            metrics.NOTIFICATIONS.labels(source_name, event.target, "failed").inc()
            continue

        log.info("sent %s/%s to %s", source_name, alert.key, event.target)
        metrics.NOTIFICATIONS.labels(source_name, event.target, "sent").inc()
        states[key] = limiter.record(current, now, target.window, event.max_per_window)
        sent = True

    return sent


async def run(cfg: settings.Settings, wait: Wait = _wait) -> None:
    """Runs every source's poll loop until SIGTERM or SIGINT.

    Args:
        cfg: Service settings.
        wait: Waits for `stop` or the timeout; injectable for tests.

    Raises:
        SystemExit: A source failed to start, or a source's credentials were rejected.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    states = state_store.load_state(cfg.state_file, datetime.datetime.now(cfg.timezone).date())
    intervals = {name: src.poll_interval or cfg.poll_interval for name, src in cfg.sources.items()}
    monitor = health.Health(intervals)
    server = await health.serve_health(monitor, cfg.health_port)

    async def poll_forever(name: str, source: message.Source, http: httpx.AsyncClient) -> None:
        while not stop.is_set():
            now = datetime.datetime.now(cfg.timezone)
            try:
                alerts = await source.poll(now)
            except message.SourceAuthError:
                raise  # exit so restarts back off; retrying every poll risks account lockout
            except (message.SourceError, httpx.HTTPError, ValueError) as exc:
                log.error("%s poll failed: %s", name, exc)
                metrics.POLLS.labels(name, "error").inc()
            else:
                if await deliver(name, alerts, cfg.targets, http, states, now):
                    state_store.prune(states, now.date())

                    try:
                        state_store.save_state(cfg.state_file, states)
                    except OSError as exc:  # keep running on in-memory state rather than crash-loop
                        log.error("cannot write state file %s: %s", cfg.state_file, exc)

                monitor.mark_ok(name)
                metrics.POLLS.labels(name, "ok").inc()
                metrics.LAST_SUCCESS.labels(name).set_to_current_time()

            await wait(stop, intervals[name])

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            built = {name: sources.build_source(name, src, http, cfg.timezone) for name, src in cfg.sources.items()}

            for name, source in built.items():
                try:
                    await source.start()
                except message.SourceAuthError as exc:
                    raise SystemExit(f"auth failed: {exc}") from None
                except (message.SourceError, httpx.HTTPError, ValueError) as exc:
                    raise SystemExit(f"{name}: start failed: {exc}") from None

            try:
                async with asyncio.TaskGroup() as tg:
                    for name, source in built.items():
                        tg.create_task(poll_forever(name, source, http))
            except* message.SourceAuthError as eg:
                raise SystemExit(f"auth failed: {eg.exceptions[0]}") from None
    finally:
        server.close()
        await server.wait_closed()
    log.info("stopped")
