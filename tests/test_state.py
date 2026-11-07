import datetime
import pathlib

import pytest

from courier import limiter
from courier import state as state_store

TODAY = datetime.date(2026, 11, 5)
S = limiter.LimiterState(TODAY, 1, datetime.datetime(2026, 11, 5, 18, tzinfo=datetime.UTC))


def test_round_trip(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "state.json"
    state = {("Rex", "battery_low:phone"): S, ("Odd/Name", "lost_mode:a/b"): limiter.LimiterState(TODAY)}
    state_store.save_state(path, state)
    assert state_store.load_state(path, TODAY) == state


def test_missing_file_empty(tmp_path: pathlib.Path) -> None:
    assert state_store.load_state(tmp_path / "nope.json", TODAY) == {}


def test_none_path_noop() -> None:
    state_store.save_state(None, {("Rex", "battery_low:phone"): S})
    assert state_store.load_state(None, TODAY) == {}


def test_corrupt_file_empty_and_logs(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert state_store.load_state(path, TODAY) == {}
    assert "state" in caplog.text


def test_load_drops_entries_before_today(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "state.json"
    old = limiter.LimiterState(datetime.date(2026, 11, 4), 1)
    state_store.save_state(path, {("Rex", "a"): old, ("Rex", "b"): S})
    assert state_store.load_state(path, TODAY) == {("Rex", "b"): S}


def test_atomic_write_no_tmp_left(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "state.json"
    state_store.save_state(path, {("Rex", "battery_low:phone"): S})
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
