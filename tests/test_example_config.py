import pathlib

import pytest

from courier import settings


def test_example_config_loads(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = pathlib.Path("config.example.yaml").read_text().replace("/run/secrets/courier", str(tmp_path))
    for name in ("ntfy-token", "fi-password", "metra-token"):
        (tmp_path / name).write_text("secret")

    config = tmp_path / "config.yaml"
    config.write_text(text)

    monkeypatch.delenv("COURIER_POLL_INTERVAL", raising=False)
    cfg = settings.load_settings(config)

    assert set(cfg.sources) == {"dogs", "commute"}
