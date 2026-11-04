"""boto3 clients (one per process) and a helper to call them from async code.

boto3 is synchronous: a call blocks the thread until AWS answers. Called directly in async code it would freeze the
whole event loop (every other request waits). `run` executes it in a worker thread instead.

Credentials and endpoints come from the standard AWS environment (no code differences between local and AWS):
- locally: AWS_ACCESS_KEY_ID/SECRET (dummy) + AWS_ENDPOINT_URL_S3/SQS pointing at the emulators;
- on ECS: no keys at all; boto3 fetches short-lived credentials of the task's IAM role automatically.
"""

import asyncio
import functools
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config

_RETRIES = Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=3, read_timeout=25)


@functools.cache
def client(service: str, region: str, endpoint_url: str | None = None) -> Any:
    """Clients are thread-safe and expensive to build: one per (service, region, endpoint)."""
    return boto3.client(service, region_name=region, endpoint_url=endpoint_url, config=_RETRIES)


async def run[T](fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    return await asyncio.to_thread(fn, *args, **kwargs)
