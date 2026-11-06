import base64
import dataclasses

import httpx
import pydantic
import pytest

from courier import message
from courier.targets import ntfy

MSG = message.Message(title="Zoë: battery 37%", body="low", priority="default", tags="battery", sid="p-battery_low")


def target(**kw: str) -> ntfy.NtfyTarget:
    return ntfy.NtfyTarget.model_validate({"type": "ntfy", "url": "https://ntfy.example.com/fi"} | kw)


async def send(t: ntfy.NtfyTarget, response: httpx.Response | Exception, msg: message.Message = MSG) -> httpx.Request:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await ntfy.send_ntfy(http, t, msg)
    return seen[0]


async def test_ntfy_bearer() -> None:
    req = await send(target(token="t"), httpx.Response(200))

    assert req.method == "POST"
    assert req.url.host == "ntfy.example.com"
    assert req.url.path == "/fi"
    assert req.headers["authorization"] == "Bearer t"
    assert req.url.params["title"] == "Zoë: battery 37%"  # Non-ASCII survives via the query string
    assert req.url.params["priority"] == "default"
    assert req.url.params["tags"] == "battery"
    assert req.url.params["sid"] == "p-battery_low"
    assert req.content == b"low"


async def test_ntfy_sid_sanitized() -> None:
    req = await send(target(), httpx.Response(200), dataclasses.replace(MSG, sid="DevAPI-4CB4 3442/x" + "a" * 100))
    assert req.url.params["sid"] == ("DevAPI-4CB4_3442_x" + "a" * 100)[-64:]


async def test_ntfy_basic() -> None:
    req = await send(target(username="u", password="p"), httpx.Response(200))
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"u:p").decode()


async def test_ntfy_no_auth() -> None:
    req = await send(target(), httpx.Response(200))
    assert "authorization" not in req.headers


async def test_ntfy_non_2xx_raises() -> None:
    with pytest.raises(ntfy.NotifyError, match="403"):
        await send(target(token="t"), httpx.Response(403))


async def test_ntfy_timeout_raises_notify_error() -> None:
    with pytest.raises(ntfy.NotifyError):
        await send(target(token="t"), httpx.ReadTimeout("slow"))


async def test_long_body_truncated() -> None:
    req = await send(target(), httpx.Response(200), dataclasses.replace(MSG, body="é" * 3000))
    assert len(req.content) <= 4000
    assert req.content.decode().endswith("…")


def test_token_and_basic_both_rejected() -> None:
    with pytest.raises(ValueError, match="not both"):
        target(token="t", username="u", password="p")


def test_basic_needs_both() -> None:
    with pytest.raises(ValueError, match="both username and password"):
        target(username="u")


def test_token_file(tmp_path) -> None:
    (tmp_path / "tok").write_text("tok\n")
    t = target(token_file=str(tmp_path / "tok"))
    assert t.token is not None
    assert t.token.get_secret_value() == "tok"


def test_secrets_hidden() -> None:
    assert "hunter2" not in repr(target(token="hunter2"))
    with pytest.raises(pydantic.ValidationError) as exc:
        target(token="hunter2", username="u", password="p")
    assert "hunter2" not in str(exc.value)
