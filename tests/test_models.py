import datetime
import pathlib

import pydantic
import pytest

from courier import models


class Secret(models.Model):
    token: str

    @pydantic.model_validator(mode="before")
    @classmethod
    def _secret(cls, data):
        return models.resolve_secret(data, "token", required=True)


def test_secret_inline() -> None:
    assert Secret.model_validate({"token": "x"}).token == "x"


def test_secret_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "t").write_text(" abc \n")
    assert Secret.model_validate({"token_file": str(tmp_path / "t")}).token == "abc"


def test_secret_both(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="token"):
        Secret.model_validate({"token": "x", "token_file": str(tmp_path / "t")})


def test_secret_neither() -> None:
    with pytest.raises(ValueError, match="token"):
        Secret.model_validate({})


def test_secret_file_missing(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="nope"):
        Secret.model_validate({"token_file": str(tmp_path / "nope")})


def test_secret_file_empty(tmp_path: pathlib.Path) -> None:
    (tmp_path / "t").write_text("  \n")
    with pytest.raises(ValueError, match="empty"):
        Secret.model_validate({"token_file": str(tmp_path / "t")})


def test_secret_file_null() -> None:
    with pytest.raises(ValueError, match="must be a file path"):
        Secret.model_validate({"token_file": None})


def test_window_days() -> None:
    window = models.Window.model_validate({"start": "08:00", "end": "20:00", "days": ["Mon", "SAT"]})
    assert window.days == {0, 5}
    assert window.start == datetime.time(8)


def test_window_single_day_string() -> None:
    window = models.Window.model_validate({"start": "08:00", "end": "20:00", "days": "mon"})
    assert window.days == {0}


def test_window_crossing_midnight_rejected() -> None:
    with pytest.raises(ValueError, match="before end"):
        models.Window.model_validate({"start": "22:00", "end": "06:00"})


@pytest.mark.parametrize("days", [[], ["funday"]])
def test_window_bad_days_rejected(days: list[str]) -> None:
    with pytest.raises(ValueError, match="days"):
        models.Window.model_validate({"start": "08:00", "end": "20:00", "days": days})


def test_event_base_rejects_zero_max_per_window() -> None:
    with pytest.raises(ValueError, match="max_per_window"):
        models.EventBase.model_validate({"target": "phone", "max_per_window": 0})


def test_source_settings_events_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        models.SourceSettings().events()
