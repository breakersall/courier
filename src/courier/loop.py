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

    Failed sends are logged, not raised, and do not stop the remaining alerts. Call only
    after a successful poll: state for this source's keys missing from `alerts` is deleted,
    so the next match of a cleared event starts a fresh budget.

    Args:
        source_name: Configured source name; first half of the state key.
        alerts: Alerts from this poll.
        targets: Delivery targets by name.
        http: HTTP client for ntfy.
        states: Limiter state; mutated in place.
        now: Current time in the configured timezone.

    Returns:
        Whether `states` changed.
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

    matching = {(source_name, a.key) for a in alerts}
    cleared = [key for key in states if key[0] == source_name and key not in matching]
    for key in cleared:
        del states[key]

    return sent or bool(cleared)


def _save(cfg: settings.Settings, states: States, now: datetime.datetime) -> None:
    state_store.prune(states, now.date())

    try:
        state_store.save_state(cfg.state_file, states)
    except OSError as exc:  # keep running on in-memory state rather than crash-loop
        log.error("cannot write state file %s: %s", cfg.state_file, exc)


class _StartError(Exception):
    """A source cannot start: its credentials or configuration are wrong."""


async def run(cfg: settings.Settings, wait: Wait = _wait) -> None:
    """Runs every source's poll loop until SIGTERM or SIGINT.

    Each source starts in its own task. A source that cannot reach its upstream retries
    every poll interval while the others run.

    Args:
        cfg: Service settings.
        wait: Waits for `stop` or the timeout; injectable for tests.

    Raises:
        SystemExit: A source's credentials or configuration were rejected at start.
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
        started = False

        while not stop.is_set():
            now = datetime.datetime.now(cfg.timezone)
            try:
                if not started:
                    await source.start()
                    started = True
                alerts = await source.poll(now)
            except (message.SourceAuthError, message.SourceConfigError) as exc:
                if not started:
                    raise _StartError(f"{name}: start failed: {exc}") from exc

                # stop only this source; retrying every poll risks account lockout
                log.error("%s credentials rejected, polling stopped until restart: %s", name, exc)
                metrics.POLLS.labels(name, "error").inc()
                return
            except (message.SourceError, httpx.HTTPError, ValueError) as exc:
                log.error("%s %s failed: %s", name, "poll" if started else "start", exc)
                metrics.POLLS.labels(name, "error").inc()
            else:
                if await deliver(name, alerts, cfg.targets, http, states, now):
                    _save(cfg, states, now)

                monitor.mark_ok(name)
                metrics.POLLS.labels(name, "ok").inc()
                metrics.LAST_SUCCESS.labels(name).set_to_current_time()

            await wait(stop, intervals[name])

    try:
        async with contextlib.AsyncExitStack() as stack:
            notify_http = await stack.enter_async_context(httpx.AsyncClient(timeout=30))

            # a client per source, so login sessions (cookies) never mix between sources
            built = {
                name: sources.build_source(
                    name, src, await stack.enter_async_context(httpx.AsyncClient(timeout=30)), cfg.timezone
                )
                for name, src in cfg.sources.items()
            }

            try:
                async with asyncio.TaskGroup() as tg:
                    for name, source in built.items():
                        tg.create_task(poll_forever(name, source, notify_http))
            except* _StartError as eg:
                raise SystemExit(str(eg.exceptions[0])) from None
    finally:
        server.close()
        await server.wait_closed()
    log.info("stopped")
