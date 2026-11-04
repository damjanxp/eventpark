"""The outbox relay: publishes committed outbox rows to SQS (`python -m eventpark.relay`).

Loop:
    BEGIN;
    SELECT ... FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED;
    SendMessageBatch per queue (max 10 per call);
    UPDATE outbox SET published_at = now() WHERE id = ANY(:sent);
    COMMIT;

- FOR UPDATE SKIP LOCKED: two relays (e.g. during a deploy, or scaled to 2) never grab the same rows. Each takes rows
  nobody else holds, so they share the work without coordinating.
- This is the one place a transaction spans a network call (SQS). It's acceptable here because the locked rows are only
  outbox rows: the API never waits for them (new outbox rows are new rows, SKIP LOCKED means other relays skip).
- Crash after SendMessageBatch but before COMMIT -> the rows stay unpublished and are sent AGAIN: at-least-once
  delivery. That is why every consumer dedupes on message_id (processed_messages).
- `outbox_lag_seconds` (age of the oldest unpublished row) is THE health signal: if it grows, events are stuck.
"""

import asyncio
import json
import signal
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.core import aws
from eventpark.core.config import Settings
from eventpark.db.models import OutboxMessage

log = structlog.get_logger()

BATCH_SIZE = 100
SQS_BATCH = 10  # SendMessageBatch accepts at most 10 entries


class SqsPublisher:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sqs = aws.client("sqs", settings.aws_region)

    def queue_url(self, routing_key: str) -> str:
        return self._settings.queue_url(routing_key)

    async def send_batch(self, queue_url: str, entries: list[tuple[int, dict[str, Any]]]) -> set[int]:
        """Returns the outbox ids SQS accepted (a batch can partially fail)."""
        response = await aws.run(
            self._sqs.send_message_batch,
            QueueUrl=queue_url,
            Entries=[{"Id": str(row_id), "MessageBody": json.dumps(payload)} for row_id, payload in entries],
        )
        for failed in response.get("Failed", []):
            log.warning("relay.send_failed", outbox_id=failed["Id"], code=failed.get("Code"))
        return {int(ok["Id"]) for ok in response.get("Successful", [])}


async def relay_once(session: AsyncSession, publisher: SqsPublisher, *, batch_size: int = BATCH_SIZE) -> int:
    """Publish one batch. Returns how many rows were published."""
    async with session.begin():
        rows = (
            await session.execute(
                select(OutboxMessage.id, OutboxMessage.routing_key, OutboxMessage.payload)
                .where(OutboxMessage.published_at.is_(None))
                .order_by(OutboxMessage.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not rows:
            return 0
        by_queue: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for row_id, routing_key, payload in rows:
            by_queue[routing_key].append((row_id, payload))
        sent: set[int] = set()
        for routing_key, entries in by_queue.items():
            url = publisher.queue_url(routing_key)
            for start in range(0, len(entries), SQS_BATCH):
                sent |= await publisher.send_batch(url, entries[start : start + SQS_BATCH])
        if sent:
            await session.execute(
                update(OutboxMessage).where(OutboxMessage.id.in_(sent)).values(published_at=func.now())
            )
    return len(sent)


async def outbox_lag_seconds(session: AsyncSession) -> float:
    oldest = await session.scalar(
        select(func.min(OutboxMessage.created_at)).where(OutboxMessage.published_at.is_(None))
    )
    await session.commit()
    return 0.0 if oldest is None else (datetime.now(UTC) - oldest).total_seconds()


async def run_relay(
    sessions: async_sessionmaker[AsyncSession],
    publisher: SqsPublisher,
    *,
    stop: asyncio.Event,
    poll_interval: float = 0.5,
    batch_size: int = BATCH_SIZE,
) -> None:
    log.info("relay.started")
    while not stop.is_set():
        try:
            async with sessions() as session:
                published = await relay_once(session, publisher, batch_size=batch_size)
                if published:
                    lag = await outbox_lag_seconds(session)
                    log.info("relay.published", count=published, outbox_lag_seconds=round(lag, 3))
        except Exception:  # DB or SQS hiccup: log, back off a bit, keep going
            log.exception("relay.error")
            published = 0
            await asyncio.sleep(2)
        if published < batch_size:  # caught up: wait a little; a full batch means more is waiting -> go again
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except TimeoutError:
                pass
    log.info("relay.stopped")


def install_stop_signals(stop: asyncio.Event) -> None:
    """SIGTERM (ECS/docker stop) and SIGINT (Ctrl+C): finish the current batch, then exit cleanly."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
