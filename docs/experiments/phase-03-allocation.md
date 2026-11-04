# Experiment: drive-up allocation under contention (Phase 3)

**Question:** 200 drivers press the ticket button at the same moment on 4 lanes; 10 drive-up places are left.
How many tickets does each allocation strategy hand out, and how fast?

**Setup:** `backend/tests/integration/test_experiment_allocation.py` (run with
`cd backend && uv run pytest -m experiment -s tests/integration/test_experiment_allocation.py`).
Real Postgres 17 (testcontainers), 20 pooled connections, so up to 20 transactions truly run in parallel.
Each variant uses a fresh event. Run on 2026-11-04 on the dev laptop (WSL2, Docker Desktop).

## Results

| Variant | Isolation | OPEN | DENY | Errors | Retries (40001) | Tickets in DB | Counter | Time |
|---|---|---|---|---|---|---|---|---|
| naive read-then-write | READ COMMITTED | **191** ❌ | 9 | 0 | 0 | **191** | **4** | 2.12 s |
| naive read-then-write | SERIALIZABLE | 10 ✅ | 190 | 0 | **1029** | 10 | 10 | 3.91 s |
| conditional `UPDATE … RETURNING` | READ COMMITTED | 10 ✅ | 190 | 0 | 0 | 10 | 10 | **1.11 s** |

## What happened

**Naive / READ COMMITTED: the lost update.** The code reads `drive_up_occupied`, compares it in Python, then writes
`occupied = <value it read> + 1`. With 20 transactions running at once:

```
t1  lane N1: SELECT occupied -> 0      (room: 0 < 10)
t2  lane N2: SELECT occupied -> 0      (room: 0 < 10)      <- N1 hasn't written yet
t3  lane N1: UPDATE SET occupied = 1   -> ticket printed
t4  lane N2: UPDATE SET occupied = 1   -> ticket printed   <- N1's +1 is overwritten ("lost")
```

Every transaction that read before the others wrote believes there is room, so **191 tickets were printed for 10
places**, and because each one wrote an *absolute* value computed from a stale read, the counter ended at **4**: it
doesn't even count its own mistakes. The `CHECK (drive_up_occupied <= capacity - prebook_quota)` constraint never
fired, because no single written value was out of range. Constraints check values, not the logic that produced them.

**Naive / SERIALIZABLE: correct, but expensive.** Postgres detects that the transactions read data another
transaction changed and aborts them with `40001 serialization_failure`. The result is correct, but it took
**1029 retries** for 200 requests and 3.5x the time of the atomic version. Every caller must implement a retry loop,
and under a real rush the retries add latency right where it hurts (the driver at the barrier).

**Conditional UPDATE: correct and fastest.**

```sql
UPDATE event_zones SET drive_up_occupied = drive_up_occupied + 1
 WHERE id = :zone AND drive_up_occupied < capacity - prebook_quota
RETURNING id;
```

The check and the write are **one statement**. Postgres locks the row while updating; a concurrent transaction
waits for that lock, then re-evaluates the `WHERE` against the *committed* new value (READ COMMITTED's
"EvalPlanQual" re-check). So exactly one transaction can take the last place; the others get 0 rows and try the next
zone or answer `CAR_PARK_FULL`. No retries, no errors, and the `+ 1` is computed by the database from the current
value, never from a stale copy in Python.

## Conclusions

- Never decide "is there room?" in application code from a value you read earlier in the transaction.
- `SERIALIZABLE` turns correctness bugs into retries: a valid tool, but here a single atomic statement is simpler,
  faster and needs no retry logic.
- Keep the `CHECK` constraint anyway: it is the safety net if someone later writes code like the naive version
  that pushes a counter *above* the limit (e.g. `SET occupied = occupied + 1` without the `WHERE`).
- The production code uses the atomic version (`services/gate.py::_allocate_atomic`); the naive one stays in the
  codebase only for this experiment and is marked as wrong on purpose.
