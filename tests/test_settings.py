import pathlib
import zoneinfo

import httpx
import pydantic
import pytest

from courier import settings
from courier import sources
from courier.sources import fi
from courier.sources import metra
from courier.sources.fi import settings as fi_settings
from courier.sources.metra import settings as metra_settings

YAML = """
poll_interval: 300
timezone: America/Chicago
targets:
  phone: {type: ntfy, url: "https://ntfy.sh/x"}
sources:
  dogs:
    type: fi
    email: a@b.c
    password: pw
    collars:
      - name: Rex
        events: [{type: lost_mode, target: phone}]
  commute:
    type: metra
    token: t
    trains:
      - line: UP-N
        number: "310"
        events: [{type: train_delayed, target: phone}]
"""


def write(tmp_path: pathlib.Path, text: str = YAML) -> pathlib.Path:
    path = tmp_path / "c.yaml"
    path.write_text(text)
    return path


def test_load_full_example(tmp_path: pathlib.Path) -> None:
    cfg = settings.load_settings(write(tmp_path))
    assert isinstance(cfg.sources["dogs"], fi_settings.FiSettings)
    assert isinstance(cfg.sources["commute"], metra_settings.MetraSettings)
    assert cfg.health_port == 8080
    assert cfg.timezone == zoneinfo.ZoneInfo("America/Chicago")


def test_env_overrides_yaml(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COURIER_POLL_INTERVAL", "600")
    cfg = settings.load_settings(write(tmp_path))
    assert cfg.poll_interval == 600
    assert set(cfg.sources) == {"dogs", "commute"}


def test_env_deep_merges_nested(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COURIER_SOURCES__COMMUTE__TOKEN", "from-env")

    cfg = settings.load_settings(write(tmp_path))
    commute = cfg.sources["commute"]
    assert isinstance(commute, metra_settings.MetraSettings)
    assert commute.token.get_secret_value() == "from-env"
    assert commute.trains[0].number == "310"


def test_unknown_source_type_rejected(tmp_path: pathlib.Path) -> None:
    with pytest.raises(pydantic.ValidationError):
        settings.load_settings(write(tmp_path, YAML.replace("type: metra", "type: bus")))


def test_unknown_event_type_rejected(tmp_path: pathlib.Path) -> None:
    with pytest.raises(pydantic.ValidationError):
        settings.load_settings(write(tmp_path, YAML.replace("type: lost_mode", "type: nope")))


def test_unknown_target_names_source(tmp_path: pathlib.Path) -> None:
    with pytest.raises(pydantic.ValidationError, match="source 'commute'"):
        settings.load_settings(
            write(tmp_path, YAML.replace("train_delayed, target: phone", "train_delayed, target: x"))
        )


def test_secret_file_resolved_inside_source(tmp_path: pathlib.Path) -> None:
    secret = tmp_path / "tok"
    secret.write_text("filetoken\n")

    cfg = settings.load_settings(write(tmp_path, YAML.replace("token: t", f"token_file: {secret}")))
    commute = cfg.sources["commute"]
    assert isinstance(commute, metra_settings.MetraSettings)
    assert commute.token.get_secret_value() == "filetoken"


def test_metra_interval_default(tmp_path: pathlib.Path) -> None:
    cfg = settings.load_settings(write(tmp_path))
    assert cfg.sources["commute"].poll_interval == 120
    assert cfg.sources["dogs"].poll_interval is None


async def test_build_source_types(tmp_path: pathlib.Path) -> None:
    cfg = settings.load_settings(write(tmp_path))

    async with httpx.AsyncClient() as http:
        assert isinstance(sources.build_source("dogs", cfg.sources["dogs"], http, cfg.timezone), fi.FiSource)
        assert isinstance(
            sources.build_source("commute", cfg.sources["commute"], http, cfg.timezone), metra.MetraSource
        )


def test_missing_file_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(FileNotFoundError, match="config file not found"):
        settings.load_settings(tmp_path / "nope.yaml")
