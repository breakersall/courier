"""ntfy target: configuration and publisher."""

import re
from typing import Any, Literal, Self

import httpx
import pydantic

from courier import message
from courier import models

_MAX_BODY_BYTES = 4000


class NtfyTarget(models.Model):
    """An ntfy topic to publish to.

    Attributes:
        type: Always "ntfy".
        url: Full topic URL.
        window: Optional delivery window; None means all day.
        token: Bearer token, exclusive with username/password.
        username: Basic-auth user.
        password: Basic-auth password.
    """

    type: Literal["ntfy"]
    url: pydantic.HttpUrl
    window: models.Window | None = None
    token: pydantic.SecretStr | None = None
    username: str | None = None
    password: pydantic.SecretStr | None = None

    @pydantic.model_validator(mode="before")
    @classmethod
    def _secrets(cls, data: Any) -> Any:
        data = models.resolve_secret(data, "token", required=False)
        return models.resolve_secret(data, "password", required=False)

    @pydantic.model_validator(mode="after")
    def _auth(self) -> Self:
        basic = self.username is not None or self.password is not None

        if self.token is not None and basic:
            raise ValueError("use either token or username/password, not both")
        if basic and (self.username is None or self.password is None):
            raise ValueError("basic auth needs both username and password")

        return self


class NotifyError(Exception):
    """Publishing to ntfy failed."""


async def send_ntfy(client: httpx.AsyncClient, target: NtfyTarget, msg: message.Message) -> None:
    """Publishes a message to an ntfy topic.

    Bodies over 4000 bytes are truncated, ending in an ellipsis.

    Args:
        client: HTTP client to use.
        target: Topic URL and credentials.
        msg: Message to publish.

    Raises:
        NotifyError: The request failed, timed out, or returned a non-2xx status.
    """
    headers: dict[str, str] = {}
    if target.token is not None:
        headers["Authorization"] = f"Bearer {target.token.get_secret_value()}"

    auth: httpx.BasicAuth | httpx._client.UseClientDefault = httpx.USE_CLIENT_DEFAULT
    if target.username is not None and target.password is not None:
        auth = httpx.BasicAuth(target.username, target.password.get_secret_value())

    # query params rather than headers so non-ASCII titles survive
    params = {"title": msg.title, "priority": msg.priority, "tags": msg.tags}

    if msg.sid:
        params["sid"] = re.sub(r"[^-_A-Za-z0-9]", "_", msg.sid)[-64:]  # ntfy-safe sequence id

    body = msg.body.encode()

    if len(body) > _MAX_BODY_BYTES:
        body = (body[: _MAX_BODY_BYTES - 3].decode(errors="ignore") + "…").encode()

    try:
        resp = await client.post(str(target.url), params=params, content=body, headers=headers, auth=auth, timeout=10)
    except httpx.HTTPError as exc:
        raise NotifyError(f"ntfy request failed: {exc!r}") from exc

    if resp.is_error:
        raise NotifyError(f"ntfy returned HTTP {resp.status_code}")
