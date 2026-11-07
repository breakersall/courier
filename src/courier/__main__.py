"""Entry point: poll every configured source and deliver notifications."""

import argparse
import json
import logging
import os
import pathlib

from courier import loop
from courier import settings


class JsonFormatter(logging.Formatter):
    """Formats a record as one JSON object; the container runtime adds the timestamp."""

    def format(self, record: logging.LogRecord) -> str:
        """Returns `record` as JSON with level, logger, message, and any exception."""
        entry = {"level": record.levelname, "logger": record.name, "message": record.getMessage()}

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry)


def main(argv: list[str] | None = None) -> None:
    """Parses arguments, loads the settings, and runs the service.

    Args:
        argv: Command-line arguments; defaults to sys.argv.

    Raises:
        SystemExit: The config is missing or invalid, or the health port is in use.
    """
    parser = argparse.ArgumentParser(prog="courier", description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, required=True)
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument("--log-format", choices=["text", "json"], default=os.environ.get("LOG_FORMAT", "text"))
    args = parser.parse_args(argv)

    handler = logging.StreamHandler()
    if args.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=args.log_level.upper(), handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        cfg = settings.load_settings(args.config)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"invalid config {args.config}: {exc}") from None

    loop.run(cfg)


if __name__ == "__main__":
    main()
