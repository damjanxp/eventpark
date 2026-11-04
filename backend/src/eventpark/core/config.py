"""Application settings, read from environment variables (12-factor config).

Every setting comes from an env var with the same name in UPPER_CASE (e.g. `log_level` <- `LOG_LEVEL`).
pydantic-settings validates them when `Settings()` is created, so a missing or malformed value stops the
process at startup with a clear error instead of failing later on the first request.

Local values live in the repo-root `.env` (gitignored; see `.env.example`). On AWS, ECS injects them.
"""

from datetime import timedelta
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr
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

    # --- auth (docs/02-architecture.md §9) ---
    # Signs access tokens (HS256). SecretStr: printing the settings shows '**********', never the value.
    # At least 32 bytes: an HMAC key shorter than the hash output (SHA-256 = 32 bytes) is weaker than it could be.
    jwt_secret: SecretStr = Field(min_length=32)
    access_token_ttl_seconds: int = Field(default=15 * 60, gt=0)
    refresh_token_ttl_days: int = Field(default=7, gt=0)
    # Secure cookies are only sent over HTTPS. Browsers treat http://localhost as secure too, so this stays on.
    auth_cookie_secure: bool = True

    # --- gate hot path ---
    # ENTERING/EXITING are temporary holds: if the car doesn't pass the loop within this time, the hold is released.
    gate_hold_seconds: int = Field(default=60, gt=0)
    # How long a PAYMENT_REQUIRED exit waits for the card before the passage expires.
    payment_pending_seconds: int = Field(default=300, gt=0)

    @property
    def access_token_ttl(self) -> timedelta:
        return timedelta(seconds=self.access_token_ttl_seconds)

    @property
    def refresh_token_ttl(self) -> timedelta:
        return timedelta(days=self.refresh_token_ttl_days)


@lru_cache
def get_settings() -> Settings:
    """Create the settings once per process and reuse them."""
    return Settings()
