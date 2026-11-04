"""Transactional outbox: record a domain event in the SAME database transaction as the change it describes.

The dual-write problem: "UPDATE ticket ... ; COMMIT; sqs.send(...)" fails half-way sometimes (network blip after the
commit -> nobody hears about the entry; or send first, then the transaction rolls back -> workers count a car that
never entered). You can't atomically write to two systems. So we write only to Postgres: the message goes into the
`outbox` table inside the transaction, and the outbox relay (Phase 6) publishes committed rows to SQS afterwards.
Result: a message is published if and only if the transaction committed (at least once; consumers dedupe).

Never call SQS (or any network service) inside a DB transaction (CLAUDE.md §3).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from eventpark.core.ids import uuid7
from eventpark.db.models import OutboxMessage

# Message type -> queue. Entry and exit events go to separate queues so each worker scales on its own backlog.
ROUTES: dict[str, str] = {
    "passage.authorized": "{direction}-events",
    "passage.completed": "{direction}-events",
    "passage.expired": "{direction}-events",
    "passage.aborted": "{direction}-events",
    "passage.rejected": "{direction}-events",
    "payment.succeeded": "exit-events",
    "booking.confirmed": "notifications",
    "booking.expired": "notifications",
    "alert.zone_threshold": "notifications",
    "alert.device_offline": "notifications",
    "alert.printer": "notifications",
    "report.ready": "notifications",
    "report.requested": "reports",
}


def routing_key(message_type: str, direction: str | None = None) -> str:
    template = ROUTES[message_type]
    if "{direction}" in template:
        if direction is None:
            raise ValueError(f"{message_type} needs a direction to be routed")
        return template.format(direction=direction.lower())
    return template


def add(
    session: AsyncSession,
    message_type: str,
    data: dict[str, Any],
    *,
    event_id: uuid.UUID | None = None,
    direction: str | None = None,
    producer: str = "api",
) -> uuid.UUID:
    """Queue a message for publishing. Call inside the use case's transaction; nothing is sent here."""
    message_id = uuid7()
    envelope = {
        "message_id": str(message_id),
        "type": message_type,
        "version": 1,
        "occurred_at": datetime.now(UTC).isoformat(),
        "event_id": str(event_id) if event_id else None,
        "data": _jsonable(data),
        # request_id lets you follow one scan from the gate through API -> outbox -> SQS -> worker logs.
        "meta": {"request_id": structlog.contextvars.get_contextvars().get("request_id"), "producer": producer},
    }
    session.add(
        OutboxMessage(
            message_id=message_id,
            type=message_type,
            routing_key=routing_key(message_type, direction),
            payload=envelope,
        )
    )
    return message_id


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):  # enums
        return value.value
    return value
