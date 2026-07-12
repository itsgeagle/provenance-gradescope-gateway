"""Runtime settings loaded from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """A required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    db_path: Path
    secret_key: str
    poll_interval_s: float = 2.0
    poll_timeout_s: float = 600.0
    http_timeout_s: float = 60.0


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise ConfigError(f"missing required environment variable {key}")
    return value


def _optional_float(env: Mapping[str, str], key: str, default: float) -> float:
    value = env.get(key)
    if not value:
        return default
    return float(value)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    # Fallbacks below must track the Settings dataclass defaults.
    return Settings(
        db_path=Path(_require(env, "PROVGATE_DB_PATH")),
        secret_key=_require(env, "PROVGATE_SECRET_KEY"),
        poll_interval_s=_optional_float(env, "PROVGATE_POLL_INTERVAL_S", 2.0),
        poll_timeout_s=_optional_float(env, "PROVGATE_POLL_TIMEOUT_S", 600.0),
        http_timeout_s=_optional_float(env, "PROVGATE_HTTP_TIMEOUT_S", 60.0),
    )
