import json
import logging
import pathlib
import sys

import pytest

from courier import __main__


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("courier", logging.ERROR, __file__, 1, "poll %s", ("dogs",), sys.exc_info())

    entry = json.loads(__main__.JsonFormatter().format(record))

    assert entry["level"] == "ERROR"
    assert entry["logger"] == "courier"
    assert entry["message"] == "poll dogs"
    assert "ValueError: boom" in entry["exception"]


def test_main_invalid_config_exits(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("poll_interval: 5\n")
    with pytest.raises(SystemExit, match=rf"invalid config {path}"):
        __main__.main(["--config", str(path)])
