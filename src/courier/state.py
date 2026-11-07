"""Optional JSON persistence of limiter state, keyed by (source name, check key)."""

import datetime
import json
import logging
import os
import pathlib

import pydantic

from courier import limiter

log = logging.getLogger(__name__)

type StateKey = tuple[str, str]

_ADAPTER = pydantic.TypeAdapter(limiter.LimiterState)


def load_state(path: pathlib.Path | None, today: datetime.date) -> dict[StateKey, limiter.LimiterState]:
    """Loads saved limiter state, ignoring an unreadable file.

    Args:
        path: State file, or None when persistence is disabled.
        today: Local date; entries from earlier days are dropped.

    Returns:
        Saved state dated today or later; empty if the file is missing or unreadable.
    """
    if path is None or not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text())
        state: dict[StateKey, limiter.LimiterState] = {}

        for key, value in raw.items():
            name, event_id = json.loads(key)
            entry = _ADAPTER.validate_python(value)

            # earlier days are spent budgets, and keys of removed sources would never clear
            if entry.window_date is not None and entry.window_date >= today:
                state[(name, event_id)] = entry
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("ignoring unreadable state file %s: %r", path, exc)
        return {}

    return state


def save_state(path: pathlib.Path | None, state: dict[StateKey, limiter.LimiterState]) -> None:
    """Atomically writes limiter state.

    Args:
        path: State file, or None when persistence is disabled (no-op).
        state: State to save.

    Raises:
        OSError: The file cannot be written.
    """
    if path is None:
        return

    raw = {json.dumps(list(key)): _ADAPTER.dump_python(s, mode="json") for key, s in state.items()}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2))
    os.replace(tmp, path)
