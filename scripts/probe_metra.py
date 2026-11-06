"""One-shot Metra probe: each realtime feed, the published stamp, and the static schedule.

Usage: uv run scripts/probe_metra.py TOKEN_FILE [OUT_DIR]   (default OUT_DIR: metra-probe-out)
Writes <feed>.json, <feed>.pb, published.txt, schedule.zip, and summary.json. Never writes the token.
"""

import asyncio
import json
import pathlib
import sys
from typing import get_args, Literal

from google.protobuf import json_format
from google.transit import gtfs_realtime_pb2
import httpx

from courier import message
from courier.sources.metra import client as metra_client

FEEDS = get_args(Literal["tripupdates", "alerts", "positions"])


async def fetch_all(token: str) -> tuple[dict[str, gtfs_realtime_pb2.FeedMessage], str, bytes, dict[str, object]]:
    """Fetches every Metra endpoint once.

    Args:
        token: Metra API token.

    Returns:
        The decoded feeds by name, the published text, the schedule zip, and a per-endpoint response summary.
    """
    summary: dict[str, object] = {}

    async def record(resp: httpx.Response) -> None:
        await resp.aread()
        name = resp.url.path.rsplit("/", 1)[-1]
        rate = {k: v for k, v in resp.headers.items() if k.startswith("x-ratelimit")}
        summary[name] = {"status": resp.status_code, "bytes": len(resp.content), "rate_limit": rate}

    async with httpx.AsyncClient(timeout=60, event_hooks={"response": [record]}) as http:
        client = metra_client.MetraClient(token, http)
        feeds = {feed: await client.fetch_feed(feed) for feed in FEEDS}
        return feeds, await client.fetch_published(), await client.fetch_schedule(), summary


def main(token_file: pathlib.Path, out: pathlib.Path) -> None:
    """Fetches every Metra endpoint once and writes the results.

    Args:
        token_file: File whose first line is the Metra API token.
        out: Output directory, created if missing.
    """
    token = token_file.expanduser().read_text().splitlines()[0].strip()
    feeds, published, schedule, summary = asyncio.run(fetch_all(token))

    out.mkdir(parents=True, exist_ok=True)
    for name, msg in feeds.items():
        (out / f"{name}.pb").write_bytes(msg.SerializeToString())
        (out / f"{name}.json").write_text(json_format.MessageToJson(msg))

    (out / "published.txt").write_text(published)
    (out / "schedule.zip").write_bytes(schedule)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    try:
        main(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "metra-probe-out"))
    except (OSError, IndexError, message.SourceError) as exc:
        sys.exit(f"probe failed: {exc}")
