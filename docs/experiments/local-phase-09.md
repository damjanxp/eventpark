# Phase 9 — simulator runs, bottlenecks, races (local, Docker Desktop on WSL2, 12 CPUs)

Reports from each run are written to `simulator/reports/` (gitignored); the numbers below are copied from them.
Latencies on this machine vary run to run by up to ~2× (everything shares one Docker VM): compare within a
section, not across sections.

## 1. rush-2000-in-15 — the design target

2,000 cars in 15 simulated minutes (60× time scale), 30 % pre-booked, 6 entry / 3 exit lanes, 8 s per car per lane.

| | |
|---|---|
| entered / exited / stuck | 2000 / 2000 / 0 |
| entry/issue p50 / p95 / p99 | 25 / 84 / 148 ms |
| entry/scan p50 / p95 / p99 | 21 / 53 / 76 ms |
| exit/scan p50 / p95 / p99 | 19 / 42 / 63 ms |
| bookings | 549 confirmed, 50 SOLD_OUT (600 drivers for 550 places: correct), 1 stuck (→ race, §3) |
| invariants | 17/17 |

**Little's Law:** 2.22 cars/s × 8 s ≈ 17.8 lanes busy on average, but there are only 6. The entry queue p50 was
~24 simulated minutes, while the API answered in ~25 ms. **The barrier is the bottleneck, not the server**: more lanes
(or faster barriers / pre-booked express lanes) is the fix, not more API tasks.

## 2. Bottleneck 1 — argon2 on the event loop (fixed)

Presale: 600 drivers sign up and book at once. A probe timed a trivial `GET /healthz` every 50 ms meanwhile.

| | /healthz p99 | /healthz max | register p95 |
|---|---:|---:|---:|
| before (argon2 called directly in async code) | 168 ms | **1,737 ms** | 2,992 ms |
| after (`asyncio.to_thread`, max 4 concurrent, outside DB transactions) | 41 ms | **82 ms** | 1,500 ms |

argon2 is slow on purpose (~tens of ms of CPU, 64 MiB). Called from a coroutine, it freezes the event loop: every
other request in that process waits, **gate decisions included**. Fix in `core/security.py` + `services/auth.py`,
regression test `test_password_hashing_does_not_block_the_event_loop`.

## 3. Race 1 — webhook faster than our commit (fixed)

Presale bookings: **27 of 600 stayed PENDING_PAYMENT** although fakepay charged them. Logs: `webhook.unknown_intent`.
fakepay sends the webhook ~0.2 s after creating the intent; under load our "save provider_ref" transaction hadn't
committed yet. The handler answered 2xx "ignored" AND recorded the event id, so the payment was dropped (until the
reconciliation job would find it minutes later). Fix (`services/webhooks.py`): match by the `booking_id` we put in the
intent's metadata; if still unknown answer **503** so nothing is recorded and the provider retries.
After: 500/500 confirmed, 100 SOLD_OUT, 3 webhooks got a 503 and succeeded on retry. Tests:
`test_webhook_that_beats_our_commit_is_matched_by_booking_id`, `test_unknown_intent_is_retryable_and_not_recorded`.

## 4. Race 2 — stale objects under a row lock (fixed, ADR-030)

`chaos` (15 % dropped answers, Redis flushed then stopped, worker + relay restarted): everything held except
**`hourly_stats` revenue 8.34 M vs 8.07 M para** in succeeded payments. Nine exit payments had **two**
`payment.succeeded` outbox messages. A dropped answer made the lane retry `exit/pay` while the first call was still at
the provider; both got "succeeded". The `if not was_succeeded` guard ran under `SELECT … FOR UPDATE`, but with
`expire_on_commit=False` the session returned the object cached from its *first* transaction (status PENDING), so both
applied the effects. Card charged once (provider idempotency), revenue counted twice. Fix: a session event adds
`populate_existing` to every locking select (`db/session.py`). After: chaos 17/17, 332 dropped answers, 0 duplicates.
Test `test_locked_reads_never_return_stale_cached_objects` fails without the fix.

## 5. Things that did NOT help (measured, kept or reverted)

| idea | booking p50 | verdict |
|---|---:|---|
| baseline (after §2) | 733 ms | |
| lock the zone row only for check + increment | 767 ms | no effect; kept (shorter lock is still right) |
| DB pool 20 + 20 instead of 5 + 10 | 1,274 ms | **worse**: more transactions fighting for the same row and CPU. Default kept, now configurable |
| 2 API instances behind Caddy | 698 ms | no effect: both containers were at ~300 % CPU hashing passwords |

A single booking takes ~30 ms. With sign-ups moved before the sale (realistic), bookings alone measured p50 546 ms at
60 concurrent: the remaining time is CPU shared by everything on one Docker VM. That needs a real load test on AWS
(Phase 14), not more local tuning.

## 6. Admission in Redis vs Postgres (`tests/integration/test_experiment_admission.py`)

500 concurrent requests for 100 places, 50 in flight:

| variant | admitted | p50 | p95 |
|---|---:|---:|---:|
| Postgres conditional `UPDATE … WHERE occupied < cap RETURNING` | 100 | 14 ms | 413 ms |
| Redis Lua (`GET` < limit → `INCR`) | 100 | 4 ms | 128 ms |
| Redis Lua, `FLUSHALL` halfway | **200** | 2 ms | 17 ms |

Both are correct while Redis keeps its data; Redis is ~4× faster. But one lost key (restart without persistence,
eviction, a flush) sells every place twice, and the ticket must still be written to Postgres: two systems to keep
consistent. 14 ms is far below what a driver notices at a barrier that takes 8 s. **Postgres stays the source of
truth for capacity** (CLAUDE.md §3); the Postgres p95 comes from 50 transactions queuing on one hot row, the place to
look if a single zone ever needs more than a few hundred admissions per second (split the counter into N rows).

## 7. Other scenarios

- `smoke` (CI): 50 cars, 5 % dropped answers, fees + declines → 17/17.
- `presale`: 600 drivers / 500 places → exactly 500 confirmed, 100 SOLD_OUT, prebook_held == active bookings.
- `stress-10k`, `fair-saturday`: written, not run yet.
