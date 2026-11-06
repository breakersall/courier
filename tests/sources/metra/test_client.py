import logging
import pathlib
from typing import Literal

import httpx
import pytest

from courier import message
from courier import metrics
from courier.sources.metra import client

FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures"


def make(
    *, status_code: int = 200, content: bytes = b"", headers: dict[str, str] | None = None
) -> tuple[client.MetraClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code, content=content, headers=headers)

    return client.MetraClient("t", httpx.AsyncClient(transport=httpx.MockTransport(respond))), seen


@pytest.mark.parametrize(("name", "count"), [("tripupdates", 19), ("alerts", 15)])
async def test_fetch_feed(name: Literal["tripupdates", "alerts"], count: int) -> None:
    c, seen = make(content=(FIXTURES / f"metra_{name}.pb").read_bytes())
    feed = await c.fetch_feed(name)

    assert len(feed.entity) == count
    assert seen[0].headers["authorization"] == "Bearer t"
    assert "api_token" not in seen[0].url.query.decode()
    assert seen[0].url.path.endswith(f"/{name}")


@pytest.mark.parametrize("status", [401, 403])
async def test_auth(status: int) -> None:
    c, _ = make(status_code=status)
    with pytest.raises(message.SourceAuthError):
        await c.fetch_feed("alerts")


async def test_server_error_is_not_auth() -> None:
    c, _ = make(status_code=500)
    with pytest.raises(message.SourceError) as exc:
        await c.fetch_feed("alerts")
    assert not isinstance(exc.value, message.SourceAuthError)


async def test_html_body() -> None:
    c, _ = make(content=b"<!doctype html>")
    with pytest.raises(message.SourceError):
        await c.fetch_feed("alerts")


async def test_rate_limit_warning(caplog: pytest.LogCaptureFixture) -> None:
    c, _ = make(content=b"", headers={"x-ratelimit-remaining": "5"})
    with caplog.at_level(logging.WARNING):
        await c.fetch_feed("alerts")
    assert "rate limit" in caplog.text
    assert metrics.REGISTRY.get_sample_value("courier_metra_ratelimit_remaining") == 5


async def test_static() -> None:
    c, _ = make(content=b" 2026\n")
    assert await c.fetch_published() == "2026"
    assert await c.fetch_schedule() == b" 2026\n"

    c, _ = make(status_code=404)
    with pytest.raises(message.SourceError):
        await c.fetch_schedule()
