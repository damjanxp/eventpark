"""What must be true after a run (learning path Phase 9). Read straight from Postgres, the source of truth.

Groups:
  capacity      counters never exceed their limits (the DB CHECKs enforce it; we verify)
  counters      every counter equals the real count of rows it summarises (no lost or double increments)
  passages      no ticket entered or left twice; ticket states match their passages
  simulator     what the cars experienced == what the DB recorded (retries/dropped answers created no extras)
  payments      nobody charged twice; every paid exit has its payment
  async         after settling: outbox drained, hourly_stats == passages, Redis read model == Postgres
"""

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import asyncpg
from redis.asyncio import Redis

from simulator.metrics import Metrics


@dataclass(frozen=True)
class Check:
    group: str
    name: str
    ok: bool
    detail: str = ""


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql://eventpark:eventpark-local-only@localhost:5433/eventpark")
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _rows(conn: asyncpg.Connection, sql: str, *args: Any) -> list[asyncpg.Record]:
    return list(await conn.fetch(sql, *args))


async def _value(conn: asyncpg.Connection, sql: str, *args: Any) -> Any:
    return await conn.fetchval(sql, *args)


def _none(group: str, name: str, rows: list[asyncpg.Record], describe: str) -> Check:
    return Check(group, name, not rows, f"{len(rows)} {describe}: {[dict(r) for r in rows[:3]]}" if rows else "")


async def static_checks(conn: asyncpg.Connection, event_id: str, metrics: Metrics | None) -> list[Check]:
    checks: list[Check] = []
    zones = await _rows(
        conn,
        """SELECT z.code, ez.* FROM event_zones ez JOIN zones z ON z.id = ez.zone_id WHERE ez.event_id = $1""",
        event_id,
    )
    over = [
        dict(code=r["code"], occupied=r["drive_up_occupied"], held=r["prebook_held"])
        for r in zones
        if r["drive_up_occupied"] > r["capacity"] - r["prebook_quota"]
        or r["prebook_held"] > r["prebook_quota"]
        or r["prebook_inside"] > r["prebook_held"]
        or min(r["drive_up_occupied"], r["prebook_held"], r["prebook_inside"]) < 0
    ]
    checks.append(Check("capacity", "no zone over its drive-up or pre-book limit", not over, str(over) if over else ""))

    counted = await _rows(
        conn,
        """
        SELECT z.code, ez.drive_up_occupied, ez.prebook_inside, ez.prebook_held,
          (SELECT count(*) FROM tickets t WHERE t.event_zone_id = ez.id AND t.type = 'DRIVE_UP'
             AND t.status IN ('ENTERING', 'INSIDE', 'EXITING')) AS real_drive_up,
          (SELECT count(*) FROM tickets t WHERE t.event_zone_id = ez.id AND t.type = 'PREBOOKED'
             AND t.status IN ('INSIDE', 'EXITING')) AS real_prebook_inside,
          (SELECT count(*) FROM bookings b WHERE b.event_zone_id = ez.id
             AND b.status IN ('PENDING_PAYMENT', 'CONFIRMED')) AS real_held
        FROM event_zones ez JOIN zones z ON z.id = ez.zone_id WHERE ez.event_id = $1""",
        event_id,
    )
    for column, real in (
        ("drive_up_occupied", "real_drive_up"),
        ("prebook_inside", "real_prebook_inside"),
        ("prebook_held", "real_held"),
    ):
        wrong = [f"{r['code']}: counter {r[column]} vs {r[real]} rows" for r in counted if r[column] != r[real]]
        checks.append(Check("counters", f"{column} == real count", not wrong, "; ".join(wrong)))

    twice = await _rows(
        conn,
        """SELECT ticket_id, direction, count(*) AS n FROM passages
           WHERE event_id = $1 AND status = 'COMPLETED' AND ticket_id IS NOT NULL
           GROUP BY ticket_id, direction HAVING count(*) > 1""",
        event_id,
    )
    checks.append(_none("passages", "no ticket entered or exited twice", twice, "tickets"))

    inside_without_entry = await _rows(
        conn,
        """SELECT t.id, t.status FROM tickets t WHERE t.event_id = $1 AND t.status IN ('INSIDE', 'EXITING', 'EXITED')
           AND NOT EXISTS (SELECT 1 FROM passages p WHERE p.ticket_id = t.id AND p.direction = 'ENTRY'
                           AND p.status = 'COMPLETED')""",
        event_id,
    )
    checks.append(
        _none("passages", "every ticket inside/exited has a completed entry", inside_without_entry, "tickets")
    )

    exited_without_exit = await _rows(
        conn,
        """SELECT t.id FROM tickets t WHERE t.event_id = $1 AND t.status = 'EXITED'
           AND NOT EXISTS (SELECT 1 FROM passages p WHERE p.ticket_id = t.id AND p.direction = 'EXIT'
                           AND p.status = 'COMPLETED')""",
        event_id,
    )
    checks.append(_none("passages", "every exited ticket has a completed exit", exited_without_exit, "tickets"))

    double_charge = await _rows(
        conn,
        """SELECT passage_id, count(*) AS n FROM payments p JOIN tickets t ON t.id = p.ticket_id
           WHERE t.event_id = $1 AND p.status = 'SUCCEEDED' AND p.passage_id IS NOT NULL
           GROUP BY passage_id HAVING count(*) > 1""",
        event_id,
    )
    checks.append(_none("payments", "no exit charged twice", double_charge, "passages"))
    double_booking = await _rows(
        conn,
        """SELECT booking_id, count(*) AS n FROM payments p JOIN bookings b ON b.id = p.booking_id
           WHERE b.event_id = $1 AND p.status = 'SUCCEEDED' GROUP BY booking_id HAVING count(*) > 1""",
        event_id,
    )
    checks.append(_none("payments", "no booking charged twice", double_booking, "bookings"))
    unpaid = await _rows(
        conn,
        """SELECT p.id, p.amount_due_minor FROM passages p WHERE p.event_id = $1 AND p.direction = 'EXIT'
           AND p.status = 'COMPLETED' AND NOT p.manual AND coalesce(p.amount_due_minor, 0) > 0
           AND NOT EXISTS (SELECT 1 FROM payments pay WHERE pay.passage_id = p.id AND pay.status = 'SUCCEEDED')""",
        event_id,
    )
    checks.append(_none("payments", "every completed exit with a fee has a succeeded payment", unpaid, "exits"))

    if metrics is not None:
        entries = await _value(
            conn,
            "SELECT count(*) FROM passages WHERE event_id = $1 AND direction = 'ENTRY' AND status = 'COMPLETED'"
            " AND NOT manual",
            event_id,
        )
        exits = await _value(
            conn,
            "SELECT count(*) FROM passages WHERE event_id = $1 AND direction = 'EXIT' AND status = 'COMPLETED'"
            " AND NOT manual",
            event_id,
        )
        checks.append(
            Check(
                "simulator",
                "completed entries == cars that drove in",
                entries == metrics.entered,
                f"db {entries} vs sim {metrics.entered}",
            )
        )
        checks.append(
            Check(
                "simulator",
                "completed exits == cars that drove out",
                exits == metrics.exited,
                f"db {exits} vs sim {metrics.exited}",
            )
        )
    return checks


async def async_checks(conn: asyncpg.Connection, redis: Redis, event_id: str, *, settle_seconds: float) -> list[Check]:
    """Eventually consistent parts: wait (up to settle_seconds) until they agree, then report."""
    deadline = asyncio.get_running_loop().time() + settle_seconds
    while True:
        checks = await _async_once(conn, redis, event_id)
        if all(c.ok for c in checks) or asyncio.get_running_loop().time() >= deadline:
            return checks
        await asyncio.sleep(1)


async def _async_once(conn: asyncpg.Connection, redis: Redis, event_id: str) -> list[Check]:
    checks: list[Check] = []
    pending = await _value(conn, "SELECT count(*) FROM outbox WHERE published_at IS NULL")
    checks.append(Check("async", "outbox fully published", pending == 0, f"{pending} unpublished"))

    stats = await conn.fetchrow(
        "SELECT coalesce(sum(entries),0) AS e, coalesce(sum(exits),0) AS x, coalesce(sum(revenue_minor),0) AS r "
        "FROM hourly_stats WHERE event_id = $1",
        event_id,
    )
    real = await conn.fetchrow(
        """SELECT
             (SELECT count(*) FROM passages WHERE event_id = $1 AND direction = 'ENTRY' AND status = 'COMPLETED') AS e,
             (SELECT count(*) FROM passages WHERE event_id = $1 AND direction = 'EXIT' AND status = 'COMPLETED') AS x,
             (SELECT coalesce(sum(p.amount_minor), 0) FROM payments p JOIN tickets t ON t.id = p.ticket_id
               WHERE t.event_id = $1 AND p.status = 'SUCCEEDED' AND p.kind IN ('EXIT_FEE', 'LOST_TICKET')) AS r""",
        event_id,
    )
    assert stats is not None and real is not None
    for key, label in (("e", "entries"), ("x", "exits"), ("r", "revenue")):
        checks.append(
            Check(
                "async",
                f"hourly_stats {label} == source rows",
                stats[key] == real[key],
                f"stats {stats[key]} vs {real[key]}",
            )
        )

    try:
        cached = await redis.hgetall(f"occ:event:{event_id}")
    except Exception as exc:  # Redis down is a legitimate chaos state; report it, don't crash
        checks.append(Check("async", "Redis read model == Postgres", False, f"Redis unavailable: {exc}"))
        return checks
    truth = {
        r["code"]: (r["inside"], r["drive_up_occupied"])
        for r in await _rows(
            conn,
            """SELECT z.code, ez.drive_up_occupied,
                 (SELECT count(*) FROM tickets t WHERE t.event_zone_id = ez.id AND t.status = 'INSIDE') AS inside
               FROM event_zones ez JOIN zones z ON z.id = ez.zone_id WHERE ez.event_id = $1""",
            event_id,
        )
    }
    model = {
        code: (json.loads(v)["inside"], json.loads(v)["drive_up_occupied"])
        for code, v in cached.items()
        if not str(code).startswith("_")  # metadata like _updated_at
    }
    checks.append(
        Check(
            "async",
            "Redis read model == Postgres",
            model == truth,
            "" if model == truth else f"redis {model} vs db {truth}",
        )
    )
    return checks


async def check(event_id: str, metrics: Metrics | None, *, settle_seconds: float = 90) -> list[Check]:
    conn = await asyncpg.connect(database_url())
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    try:
        static = await static_checks(conn, event_id, metrics)
        return static + await async_checks(conn, redis, event_id, settle_seconds=settle_seconds)
    finally:
        await conn.close()
        await redis.aclose()
