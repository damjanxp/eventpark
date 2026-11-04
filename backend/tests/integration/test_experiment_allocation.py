"""Phase 3 experiment: drive-up allocation strategies under contention (docs/04-learning-path.md, Phase 3).

    uv run pytest -m experiment -s tests/integration/test_experiment_allocation.py

200 concurrent ticket-button presses for 10 free places, three ways:
  1. naive read-then-write, READ COMMITTED  -> expected: more than 10 tickets (lost update)
  2. naive read-then-write, SERIALIZABLE   -> expected: correct, but many 40001 aborts that need retries
  3. conditional UPDATE ... RETURNING, READ COMMITTED -> expected: exactly 10, no errors, fast
Results are printed as a table; docs/experiments/phase-03-allocation.md records a run.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from eventpark.core.config import Settings
from eventpark.db.models import EventZone, Ticket
from eventpark.domain.tickets import TicketType
from eventpark.services import gate
from eventpark.services.gate import AllocationStrategy, GateSettings

from .gate_world import build_world

pytestmark = [pytest.mark.integration, pytest.mark.experiment]

REQUESTS, FREE_PLACES, MAX_RETRIES = 200, 10, 50
CFG = GateSettings(hold=timedelta(seconds=60), payment_window=timedelta(minutes=5))


@dataclass
class Outcome:
    variant: str
    open: int
    deny: int
    errors: int
    retries: int
    tickets: int
    counter: int
    seconds: float


async def _run(settings: Settings, strategy: AllocationStrategy, isolation: str) -> Outcome:
    setup_engine = create_async_engine(str(settings.database_url))
    world = await build_world(
        async_sessionmaker(setup_engine, expire_on_commit=False),
        zones={"A": (FREE_PLACES + 2, 2, 1)},
        entry_gates=("N1", "N2", "N3", "N4"),
    )
    async with AsyncSession(setup_engine) as session:
        devices = [await gate.authenticate_device(session, world.keys[f"N{i}"]) for i in range(1, 5)]
    engine = create_async_engine(str(settings.database_url), pool_size=20, max_overflow=0, isolation_level=isolation)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    stats = {"OPEN": 0, "DENY": 0, "errors": 0, "retries": 0}

    async def press(i: int) -> None:
        request_id = str(uuid.uuid4())
        for _attempt in range(MAX_RETRIES):
            try:
                async with sessions() as session:
                    decision = await gate.entry_issue(
                        session,
                        devices[i % 4],
                        request_id=request_id,
                        now=datetime.now(UTC),
                        cfg=CFG,
                        strategy=strategy,
                    )
                stats[decision.decision.value] += 1
                return
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == "40001" or "40001" in str(exc.orig):
                    stats["retries"] += 1  # serialization_failure: Postgres aborted us, the client must retry
                    continue
                stats["errors"] += 1
                return
        stats["errors"] += 1

    started = time.perf_counter()
    await asyncio.gather(*(press(i) for i in range(REQUESTS)))
    seconds = time.perf_counter() - started
    async with AsyncSession(setup_engine) as session:
        tickets = await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.event_id == world.event_id, Ticket.type == TicketType.DRIVE_UP)
        )
        counter = (await session.get(EventZone, world.zone_ids["A"])).drive_up_occupied  # type: ignore[union-attr]
    await engine.dispose()
    await setup_engine.dispose()
    return Outcome(
        f"{strategy} / {isolation}",
        stats["OPEN"],
        stats["DENY"],
        stats["errors"],
        stats["retries"],
        tickets or 0,
        counter,
        seconds,
    )


async def test_allocation_strategies(settings: Settings) -> None:
    outcomes = [
        await _run(settings, "naive", "READ COMMITTED"),
        await _run(settings, "naive", "SERIALIZABLE"),
        await _run(settings, "atomic", "READ COMMITTED"),
    ]
    print(f"\n{REQUESTS} concurrent requests, {FREE_PLACES} free places")
    header = ("variant", "OPEN", "DENY", "errors", "retries", "tickets", "counter", "time")
    print(f"{header[0]:<30}" + "".join(f"{h:>8}" for h in header[1:]))
    for o in outcomes:
        values = (o.open, o.deny, o.errors, o.retries, o.tickets, o.counter)
        print(f"{o.variant:<30}" + "".join(f"{v:>8}" for v in values) + f"{o.seconds:>7.2f}s")

    naive_rc, naive_ser, atomic = outcomes
    assert naive_rc.tickets > FREE_PLACES or naive_rc.tickets != naive_rc.counter, "lost update didn't show this time"
    assert naive_ser.tickets == FREE_PLACES == naive_ser.counter
    assert atomic.tickets == FREE_PLACES == atomic.counter and atomic.errors == 0 and atomic.retries == 0
