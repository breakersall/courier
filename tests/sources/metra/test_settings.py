import pathlib
from typing import Any

import pydantic
import pytest

from courier.sources.metra import settings


def raw(**kw: Any) -> dict[str, Any]:
    return {
        "type": "metra",
        "token": "t",
        "trains": [
            {
                "line": "UP-N",
                "number": "310",
                "events": [
                    {"type": "train_delayed", "target": "p"},
                    {"type": "train_cancelled", "target": "p"},
                ],
            }
        ],
        "lines": [{"id": "UP-N", "events": [{"type": "service_alert", "target": "p"}]}],
    } | kw


def test_defaults() -> None:
    cfg = settings.MetraSettings.model_validate(raw())
    delayed, cancelled = cfg.trains[0].events

    assert cfg.poll_interval == 120
    assert isinstance(delayed, settings.TrainDelayed)
    assert delayed.minutes == 5
    assert delayed.max_per_window is None
    assert cancelled.max_per_window == 1
    assert cfg.lines[0].events[0].max_per_window == 1
    assert len(list(cfg.events())) == 3


def test_duplicate_train() -> None:
    train = raw()["trains"][0]
    with pytest.raises(pydantic.ValidationError, match="duplicate"):
        settings.MetraSettings.model_validate(raw(trains=[train, train]))


def test_duplicate_line() -> None:
    line = raw()["lines"][0]
    with pytest.raises(pydantic.ValidationError, match="duplicate"):
        settings.MetraSettings.model_validate(raw(lines=[line, line]))


def test_token_file(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "tok"
    f.write_text("secret\n")
    data = {k: v for k, v in raw().items() if k != "token"} | {"token_file": str(f)}
    assert settings.MetraSettings.model_validate(data).token.get_secret_value() == "secret"
