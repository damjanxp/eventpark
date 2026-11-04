"""The idempotent SQS consumer loop shared by all workers (docs/02-architecture.md §6 "Consumer loop").

For each message:
    1. already in processed_messages for this consumer?  -> delete it, done (a duplicate)
    2. handler.side_effects(msg)       OUTSIDE any transaction: S3 uploads, emails (never in a DB transaction)
    3. BEGIN; INSERT processed_messages ON CONFLICT DO NOTHING; handler.apply(session, msg); COMMIT
       (the dedupe row and the DB effects commit together: both or neither)
    4. handler.after_commit(msg)       idempotent-by-nature effects: SET absolute values in Redis, PUBLISH
    5. DeleteMessage
Any exception -> the message is NOT deleted; SQS makes it visible again after the visibility timeout and we retry.
After maxReceiveCount (5) failed receives SQS moves it to the queue's DLQ, where it waits for a human.

Exactly-once *effects* in the database, at-least-once for external side effects: an email may (rarely) be sent twice if
we crash between sending it and step 3. Sending none would be worse.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, ClassVar

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.core import aws
from eventpark.core.config import Settings
from eventpark.db.models import ProcessedMessage
from eventpark.messaging.schemas import Envelope

log = structlog.get_logger()


class Handler:
    """Subclass per worker. Override what you need; everything has a safe default."""

    name: ClassVar[str]  # consumer name in processed_messages (each consumer dedupes on its own)
    queue: ClassVar[str]  # routing key / queue name, e.g. "entry-events"
    # Long jobs (reports) keep extending their visibility timeout so SQS doesn't hand the message to another worker.
    extend_visibility_seconds: ClassVar[int | None] = None

    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings

    def wants(self, msg: Envelope) -> bool:
        return True

    async def side_effects(self, msg: Envelope) -> None:
        return None

    async def apply(self, session: AsyncSession, msg: Envelope) -> None:
        return None

    async def after_commit(self, msg: Envelope) -> None:
        return None


@dataclass
class ConsumerStats:
    processed: int = 0
    duplicates: int = 0
    failed: int = 0


async def process_one(handler: Handler, msg: Envelope, *, consumer: str | None = None) -> bool:
    """Steps 1-4 for one message. Returns False if it was a duplicate. Raises on failure (-> not deleted)."""
    name = consumer or handler.name
    async with handler.sessions() as session:
        async with session.begin():
            done = await session.scalar(
                select(ProcessedMessage.message_id).where(
                    ProcessedMessage.consumer == name, ProcessedMessage.message_id == msg.message_id
                )
            )
        if done is not None:
            return False
        if handler.wants(msg):
            await handler.side_effects(msg)
        async with session.begin():
            inserted = await session.scalar(
                insert(ProcessedMessage)
                .values(consumer=name, message_id=msg.message_id)
                .on_conflict_do_nothing()
                .returning(ProcessedMessage.message_id)
            )
            if inserted is None:  # another worker finished the same message a moment ago
                return False
            if handler.wants(msg):
                await handler.apply(session, msg)
    if handler.wants(msg):
        await handler.after_commit(msg)
    return True


class Consumer:
    def __init__(self, handler: Handler, *, wait_seconds: int = 20, max_messages: int = 10) -> None:
        self.handler = handler
        self.queue_url = handler.settings.queue_url(handler.queue)
        self.sqs = aws.client("sqs", handler.settings.aws_region)
        self.wait_seconds, self.max_messages = wait_seconds, max_messages
        self.stats = ConsumerStats()

    async def poll_once(self) -> int:
        # Long polling: SQS holds the request open up to `wait_seconds` until a message arrives. Far fewer (billed)
        # empty receives than polling in a tight loop, and messages are picked up the moment they arrive.
        response = await aws.run(
            self.sqs.receive_message,
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=self.max_messages,
            WaitTimeSeconds=self.wait_seconds,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages: list[dict[str, Any]] = response.get("Messages", [])
        for raw in messages:
            await self._handle(raw)
        return len(messages)

    async def _handle(self, raw: dict[str, Any]) -> None:
        receipt = raw["ReceiptHandle"]
        try:
            msg = Envelope.model_validate(json.loads(raw["Body"]))
        except (ValueError, ValidationError):
            # A malformed message will never succeed: leave it; after 5 receives it lands in the DLQ for inspection.
            log.error("consumer.malformed_message", queue=self.handler.queue, sqs_id=raw.get("MessageId"))
            self.stats.failed += 1
            return
        bound = structlog.contextvars.bound_contextvars(
            request_id=msg.meta.request_id,
            message_id=str(msg.message_id),
            message_type=msg.type,
            event_id=str(msg.event_id) if msg.event_id else None,
        )
        with bound:
            extender = self._start_extender(receipt)
            try:
                fresh = await process_one(self.handler, msg)
            except Exception:
                self.stats.failed += 1
                log.exception(
                    "consumer.failed",
                    receive_count=raw.get("Attributes", {}).get("ApproximateReceiveCount"),
                    worker=self.handler.name,
                )
                return  # not deleted -> redelivered after the visibility timeout
            finally:
                if extender is not None:
                    extender.cancel()
            await aws.run(self.sqs.delete_message, QueueUrl=self.queue_url, ReceiptHandle=receipt)
            if fresh:
                self.stats.processed += 1
                log.info("consumer.processed", worker=self.handler.name)
            else:
                self.stats.duplicates += 1
                log.info("consumer.duplicate_skipped", worker=self.handler.name)

    def _start_extender(self, receipt: str) -> asyncio.Task[None] | None:
        seconds = self.handler.extend_visibility_seconds
        if not seconds:
            return None

        async def extend() -> None:
            while True:
                await asyncio.sleep(seconds / 2)
                await aws.run(
                    self.sqs.change_message_visibility,
                    QueueUrl=self.queue_url,
                    ReceiptHandle=receipt,
                    VisibilityTimeout=seconds,
                )
                log.info("consumer.visibility_extended", seconds=seconds)

        return asyncio.get_running_loop().create_task(extend())

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until SIGTERM; the batch in hand is finished before exiting (graceful shutdown)."""
        log.info("consumer.started", worker=self.handler.name, queue=self.handler.queue)
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                log.exception("consumer.poll_error", worker=self.handler.name)
                await asyncio.sleep(2)
        log.info("consumer.stopped", worker=self.handler.name, **self.stats.__dict__)
