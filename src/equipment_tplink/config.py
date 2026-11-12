"""Settings: process environment > ``--env-file`` > ``./.env`` > defaults (SPEC §4)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

ENV_VARS: dict[str, str] = {
    "TPLINK_USERNAME": "tplink_username",
    "TPLINK_PASSWORD": "tplink_password",
    "EQUIPMENT_TPLINK_HOST": "host",
    "EQUIPMENT_TPLINK_PORT": "port",
    "EQUIPMENT_TPLINK_LOG_LEVEL": "log_level",
    "EQUIPMENT_TPLINK_DISCOVERY_TARGET": "discovery_target",
    "EQUIPMENT_TPLINK_DISCOVERY_TIMEOUT_S": "discovery_timeout_s",
    "EQUIPMENT_TPLINK_LOCAL_TIMEOUT_S": "local_timeout_s",
    "EQUIPMENT_TPLINK_CLOUD_TIMEOUT_S": "cloud_timeout_s",
    "EQUIPMENT_TPLINK_RELAY_SETTLE_MS": "relay_settle_ms",
}


class Settings(BaseModel):
    """Validated configuration. Secrets are excluded from ``repr``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tplink_username: str | None = Field(default=None, repr=False)
    tplink_password: str | None = Field(default=None, repr=False)
    host: str = "0.0.0.0"  # binds the LAN by design (SPEC §12)
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"
    discovery_target: str = "255.255.255.255"
    discovery_timeout_s: float = Field(default=5.0, gt=0, le=30)
    local_timeout_s: float = Field(default=10.0, gt=0, le=120)
    cloud_timeout_s: float = Field(default=20.0, gt=0, le=120)
    relay_settle_ms: int = Field(default=500, ge=0, le=10_000)

    @property
    def cloud_configured(self) -> bool:
        """Whether both cloud credentials are set."""
        return bool(self.tplink_username and self.tplink_password)


def load_settings(
    env_file: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Settings:
    """Merge the sources in precedence order and validate.

    Raises:
        FileNotFoundError: ``env_file`` was given but does not exist.
    """
    sources: list[Mapping[str, str | None]] = []
    local_env = (cwd or Path.cwd()) / ".env"
    if local_env.is_file():
        sources.append(dotenv_values(local_env))
    if env_file is not None:
        if not env_file.is_file():
            raise FileNotFoundError(f"env file not found: {env_file}")
        sources.append(dotenv_values(env_file))
    sources.append(os.environ if environ is None else environ)
    data: dict[str, str] = {}
    for source in sources:  # later sources win; empty values count as unset
        data.update({ENV_VARS[k]: v for k, v in source.items() if k in ENV_VARS and v})
    return Settings.model_validate(data)
