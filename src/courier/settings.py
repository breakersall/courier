"""Root settings loaded from a YAML file with COURIER_* environment overrides."""

import pathlib
from typing import Self
import zoneinfo

import pydantic
import pydantic_settings

from courier import sources
from courier.targets import ntfy


class Settings(pydantic_settings.BaseSettings):
    """Courier settings.

    Attributes:
        poll_interval: Default seconds between polls.
        timezone: Zone for delivery windows and times shown in messages.
        state_file: Where to persist state; None disables persistence.
        health_port: Port for the health endpoint.
        targets: Delivery targets by name.
        sources: Sources by name.
    """

    model_config = pydantic_settings.SettingsConfigDict(
        env_prefix="COURIER_",
        env_nested_delimiter="__",
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
    )

    poll_interval: int = pydantic.Field(ge=60)
    timezone: zoneinfo.ZoneInfo
    state_file: pathlib.Path | None = None
    health_port: int = pydantic.Field(default=8080, ge=1, le=65535)
    targets: dict[str, ntfy.NtfyTarget]
    sources: dict[str, sources.SourceConfig]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[pydantic_settings.BaseSettings],
        init_settings: pydantic_settings.PydanticBaseSettingsSource,
        env_settings: pydantic_settings.PydanticBaseSettingsSource,
        dotenv_settings: pydantic_settings.PydanticBaseSettingsSource,
        file_secret_settings: pydantic_settings.PydanticBaseSettingsSource,
    ) -> tuple[pydantic_settings.PydanticBaseSettingsSource, ...]:
        """Returns the sources in priority order: environment, then keyword arguments (the YAML file)."""
        return (env_settings, init_settings)

    @pydantic.model_validator(mode="after")
    def _known_targets(self) -> Self:
        for name, source in self.sources.items():
            for event in source.events():
                if event.target not in self.targets:
                    raise ValueError(f"source {name!r}: unknown target {event.target!r}")
        return self


def load_settings(path: pathlib.Path) -> Settings:
    """Loads settings from a YAML file, with environment variables taking precedence.

    Args:
        path: YAML settings file.

    Returns:
        The validated settings.

    Raises:
        FileNotFoundError: `path` is not a file.
    """
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")

    return Settings(**pydantic_settings.YamlConfigSettingsSource(Settings, yaml_file=path)())
