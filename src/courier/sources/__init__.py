"""Notification sources."""

from typing import Annotated, assert_never
import zoneinfo

import httpx
import pydantic

from courier import message
from courier.sources import fi
from courier.sources import metra
from courier.sources.fi import settings as fi_settings
from courier.sources.metra import settings as metra_settings

type SourceConfig = Annotated[
    fi_settings.FiSettings | metra_settings.MetraSettings, pydantic.Field(discriminator="type")
]


def build_source(name: str, cfg: SourceConfig, http: httpx.AsyncClient, tz: zoneinfo.ZoneInfo) -> message.Source:
    """Builds the source for a config entry.

    Args:
        name: Configured source name.
        cfg: Source settings.
        http: Shared HTTP client.
        tz: Zone for times shown in messages.

    Returns:
        The source instance.
    """
    match cfg:
        case fi_settings.FiSettings():
            return fi.FiSource(cfg, http, tz)
        case metra_settings.MetraSettings():
            return metra.MetraSource(cfg, http, tz)
        case _:
            assert_never(cfg)
