"""`python -m eventpark.ticker`: the local stand-in for EventBridge Scheduler.

Every TICK_SECONDS (default 60) it sends one {"type": "tick"} message straight to the maintenance queue. On AWS this
process doesn't exist: an EventBridge Scheduler rule does the same (Phase 12), so nothing in the workers changes.
"""

import asyncio
import json
import os
from datetime import UTC, datetime

import structlog

from eventpark.core import aws, heartbeat
from eventpark.core.config import get_settings
from eventpark.core.ids import uuid7
from eventpark.core.logging import configure_logging
from eventpark.messaging.relay import install_stop_signals

log = structlog.get_logger()


def tick_message() -> str:
    return json.dumps(
        {
            "message_id": str(uuid7()),
            "type": "tick",
            "version": 1,
            "occurred_at": datetime.now(UTC).isoformat(),
            "data": {},
            "meta": {"producer": "ticker"},
        }
    )


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    sqs = aws.client("sqs", settings.aws_region)
    queue_url = settings.queue_url("maintenance")
    interval = float(os.environ.get("TICK_SECONDS", "60"))
    stop = asyncio.Event()
    install_stop_signals(stop)
    while not stop.is_set():
        heartbeat.beat()  # HEARTBEAT_MAX_AGE must be > TICK_SECONDS (compose sets it)
        try:
            await aws.run(sqs.send_message, QueueUrl=queue_url, MessageBody=tick_message())
            log.info("ticker.tick")
        except Exception:
            log.exception("ticker.send_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
