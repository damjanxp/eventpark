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

    # --- payments (fakepay locally; a real provider would be the same shape) ---
    fakepay_url: str = "http://localhost:8100"  # where the API reaches the provider (server to server)
    fakepay_public_url: str = "http://localhost:8100"  # where a BROWSER reaches the checkout page
    fakepay_webhook_secret: SecretStr = Field(min_length=16)
    # Where the provider must send webhooks to reach this API, and where the browser returns after checkout.
    webhook_base_url: str = "http://localhost:8000"
    public_web_url: str = "http://localhost:5173"
    terminal_timeout_seconds: float = Field(default=5.0, gt=0)  # exit-lane card charge: never wait longer
    booking_hold_minutes: int = Field(default=15, gt=0)

    # --- AWS (S3, SQS) ---
    # Endpoints are NOT settings here: boto3 reads AWS_ENDPOINT_URL_S3 / AWS_ENDPOINT_URL_SQS by itself. Locally they
    # point at the emulators; on AWS they're simply not set. Same code, different config (CLAUDE.md §3).
    aws_region: str = "eu-central-1"
    bucket_files: str = "eventpark-files"
    # Presigned URLs embed the host in their signature: sign with the address the BROWSER uses (docs/02 §14 gotcha).
    s3_public_endpoint_url: str | None = None
    # Queue URLs (Terraform outputs on AWS). Only the relay and workers need them, so they're optional for the API.
    queue_url_entry_events: str | None = None
    queue_url_exit_events: str | None = None
    queue_url_notifications: str | None = None
    queue_url_reports: str | None = None
    queue_url_maintenance: str | None = None

    # Proxies in front of the API that append to X-Forwarded-For (0 locally; CloudFront + ALB on AWS = 2).
    trusted_proxy_hops: int = Field(default=0, ge=0)

    # --- email ---
    smtp_url: str | None = None  # smtp://mailpit:1025 locally; unset = emails are logged, not sent
    mail_from: str = "EventPark <noreply@eventpark.dev>"

    def queue_url(self, name: str) -> str:
        url = getattr(self, f"queue_url_{name.replace('-', '_')}", None)
        if not url:
            raise RuntimeError(f"QUEUE_URL_{name.replace('-', '_').upper()} is not configured")
        return str(url)

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
