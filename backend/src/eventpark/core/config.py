"""Application settings, read from environment variables (12-factor config).

Every setting comes from an env var with the same name in UPPER_CASE (e.g. `log_level` <- `LOG_LEVEL`).
pydantic-settings validates them when `Settings()` is created, so a missing or malformed value stops the
process at startup with a clear error instead of failing later on the first request.

Local values live in the repo-root `.env` (gitignored; see `.env.example`). On AWS, ECS injects them.
"""

from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # No env_file here on purpose: whoever starts the process provides the environment
        # (docker compose locally, `uv run --env-file ../.env` on the host, ECS on AWS).
        extra="ignore",
    )

    # Which process this is (api, outbox-relay, worker-entry, ...). Goes into every log line.
    service_name: str = "api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # "json" for machines (CloudWatch), "console" for a human reading a terminal.
    log_format: Literal["json", "console"] = "json"

    # No defaults: without a database or Redis the app is misconfigured, so it must refuse to start.
    # The Dsn types check the format (scheme, host, port) at startup too.
    database_url: PostgresDsn  # postgresql+asyncpg://user:password@host:5432/dbname
    redis_url: RedisDsn  # redis://host:6379/0   (rediss:// = TLS, used on AWS)


@lru_cache
def get_settings() -> Settings:
    """Create the settings once per process and reuse them."""
    return Settings()
