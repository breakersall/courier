# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

- What courier does, config, deployment: README.md
- Tasks: `justfile` (`just check` = what CI runs); lint rules: `pyproject.toml`
- Single test: `uv run pytest tests/test_loop.py::test_name`
- uv docs index: https://docs.astral.sh/uv/llms.txt

## Architecture

- `loop.run`: loads limiter state, builds sources (`sources.build_source`), runs one `poll_forever` task per source plus the health server in a `TaskGroup`; each poll task calls `start()` then polls. One `httpx.AsyncClient` per source (separate cookie jars) plus one for targets. Shared: the `states` dict, the health monitor.
- HTTP: `web.serve` runs a Starlette app on uvicorn with uvicorn's signal capture disabled (courier owns SIGTERM/SIGINT); bind failure → `web.ServeError` → process exits. `health.app` (`/healthz`, `/readyz`, `/metrics`) on `health_port`.
- Source contract: `message.Source` protocol (`start`, `poll(now) -> list[Check]`): a `Check` per configured event, matched or not, each with a one-line `reason`.
- Delivery: `loop.deliver` → `limiter.blocked` → `targets/ntfy.py` → `limiter.record`; logs every decision with its reason (held and unmatched at DEBUG). State key `(source_name, check.key)`, persisted by `state.py`.
- `loop.deliver` runs only after a successful poll; it deletes state for that source's keys that did not match, so a cleared event's next match starts a fresh budget.
- `check.key` comes from `message.event_keys`; changing its format resets every rate-limit budget.
- Source package layout (`sources/fi`, `sources/metra`):
  - `settings.py`: pydantic models, events are a discriminated union on `type`
  - `client.py`: upstream HTTP and parsing
  - `events.py`: pure evaluators, status + event → `(Message | None, reason)`
  - `__init__.py`: the `*Source` class
- Metrics: shared ones (`source`/`target` label) in `metrics.py`; ones only one source or target produces in that package's `client.py`, on `metrics.REGISTRY`.
- New source: add to `SourceConfig` and the `match` in `sources/__init__.py`.
- Shared config bases (`EventBase`, `Window`, `_file` secrets): `models.py`. Root settings: `settings.py`.
- Errors (caught in `loop.run`, never in sources; sources never raise `SystemExit`):
  - `SourceAuthError`, `SourceConfigError` in `start()` → process exits
  - `SourceAuthError` in a poll → that source stops until restart (avoids account lockout); others keep running
  - `SourceError`, `httpx.HTTPError`, `ValueError` in `start()` → logged, retried next interval
  - `SourceError`, `httpx.HTTPError`, `ValueError` in a poll → logged, poll skipped
  - failed send → logged, remaining alerts still sent

## Code style

- Google Python Style Guide, Google docstrings.
- Import modules, not names (`from courier import limiter` → `limiter.allowed`). No relative imports.
- Blank line between logical steps of a function body (setup, guards, main work, return). No dense blocks.
- Tests: pytest + pytest-asyncio; upstream fixtures in `tests/fixtures/`.

## Documentation and comments

- Human-facing docs (README.md):
  - concise technical English for engineers: plain words, full sentences, no over-explaining
  - title case for headers, table column headers, link titles
  - overviews (Quick Start, intros) give the one common way; alternatives and details go in their own section, linked
- Agent-facing docs (this file): lists, short, accurate, no prose.
- Describe current state only:
  - no change history ("now", "no longer", "was", "new")
  - no opinions or asides ("mind the", "useful", "nice")
- State each fact once, in its canonical place; link to it elsewhere. Config keys and defaults → `settings.py` models; user behaviour → README.md; tasks → `justfile`.
