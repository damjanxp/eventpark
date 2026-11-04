"""The message envelope every queue message uses (docs/02-architecture.md §6)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Meta(BaseModel):
    request_id: str | None = None  # the request that caused it: follow one scan through every log
    producer: str = "api"


class Envelope(BaseModel):
    message_id: uuid.UUID  # dedupe key for consumers (processed_messages)
    type: str  # e.g. "passage.completed"
    version: int = 1  # bump when `data` changes incompatibly; consumers can handle both during a rollout
    occurred_at: datetime
    event_id: uuid.UUID | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    meta: Meta = Field(default_factory=Meta)
