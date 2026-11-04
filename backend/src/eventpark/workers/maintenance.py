"""worker-maintenance: runs the scheduled jobs (services/maintenance.py) on every `tick` message.

The tick comes from a scheduler, not from user actions: EventBridge Scheduler on AWS, the tiny `ticker` container
locally. Both just drop {"type": "tick"} into the maintenance queue once a minute. A queue (instead of a cron inside
the worker) means: if the worker is down, ticks wait; if two workers run, each tick is handled once.
"""

from datetime import UTC, datetime, timedelta
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventpark.core.config import Settings
from eventpark.messaging.schemas import Envelope
from eventpark.services.gate import GateSettings
from eventpark.services.maintenance import run_tick
from eventpark.services.provider import PaymentProvider
from eventpark.workers.consumer import Handler


class Maintenance(Handler):
    name: ClassVar[str] = "worker-maintenance"
    queue: ClassVar[str] = "maintenance"

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], settings: Settings, *, provider: PaymentProvider | None = None
    ) -> None:
        super().__init__(sessions, settings)
        self.provider = provider or PaymentProvider(
            settings.fakepay_url, terminal_timeout=settings.terminal_timeout_seconds
        )
        self.cfg = GateSettings(
            hold=timedelta(seconds=settings.gate_hold_seconds),
            payment_window=timedelta(seconds=settings.payment_pending_seconds),
        )

    def wants(self, msg: Envelope) -> bool:
        return msg.type == "tick"

    async def side_effects(self, msg: Envelope) -> None:
        # The jobs commit their own small transactions and call the provider: they belong outside the dedupe tx.
        # A tick processed twice is harmless anyway (every job is idempotent).
        await run_tick(self.sessions, self.provider, self.cfg, datetime.now(UTC), self.redis)
