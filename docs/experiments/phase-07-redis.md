# Phase 7 experiments — Redis

Setup: `docker compose --profile scale up -d --wait && make seed` (api :8000, api-2 :8001, Caddy :8080).
Script for the gate traffic: one drive-up car = `POST /gate/v1/entry/issue` + `.../complete` on gate N1.

## 1. Live updates across two API instances

`uv run scripts/demo_two_instances.py`:

```
logged in via load balancer (answered by api:8000)
WebSocket open on instance A; occupancy now 0 (source: postgres)
instance B: N1 issued a ticket -> OPEN zone A
instance B: passage completed (car drove through)
instance A pushed occupancy 1 after 162 ms  ✔ fan-out works
```

Instance A never saw the gate request. The path was B → Postgres (passage + outbox row, one transaction) → relay →
SQS → worker-entry → Redis `PUBLISH realtime:event:{id}` → **every** API instance's subscriber → A's socket.
162 ms end to end, most of it the relay poll interval (0.5 s max) and the SQS long poll handing over.
Without pub/sub, A would only learn about it if the browser happened to be connected to B.

## 2. Flush Redis during traffic

`FLUSHALL` between cars. Gate decisions unaffected (~23 ms before and after: Redis isn't on the decision path except
for the rate limiter and the cached event config, which simply miss). The occupancy read model was back **0.4 s**
later — the next passage message made worker-entry write absolute values from Postgres. With no traffic, the
maintenance tick (≤ 60 s) rebuilds it. Rate-limit buckets restart full (a flush forgives everyone: acceptable).

## 3. Stop Redis entirely — gates must still work

| | per-car latency |
|---|---|
| Redis up | ~24 ms |
| Redis stopped, **before** the fix | 2037, 4113, 4056, 4049, 4048 ms |
| Redis stopped, after ADR-027 (0.25 s timeout + circuit breaker) | 450, 30, 36, 34, 24 ms |

Every car got OPEN in both cases, so "fail open" was correct, but at 2–4 s the gate controller (2 s timeout) would
have retried every request: lanes stall. The cause: `socket_connect_timeout=2` paid on *each* Redis call (rate limiter +
event config cache) of *each* request. Fix: fail fast — short timeout, then a breaker that skips Redis for 5 s after a
failure. Regression tests: `tests/test_redis_breaker.py`, `test_gates_stay_fast_when_redis_hangs`. (The older test used
a closed port, which refuses instantly and hid the problem; the new tests use a non-routable address that hangs.)

Public event page: 200 (served from Postgres). `POST /admin/v1/ws-ticket` → 503 REALTIME_UNAVAILABLE; the dashboard
falls back to polling (Phase 8).

## 4. Hammer the login endpoint

15 wrong-password logins for one email: `401 ×5`, then `429 ×10`, `Retry-After: 12`. The per-email bucket (5 burst)
stops guessing against one account; the per-IP bucket stops one machine trying many accounts.

## Takeaways

- A cache that is down must be *fast* to skip, not just skippable. Measure the outage path, don't just assert it works.
- Test the failure you'll really get: "connection refused" ≠ "no answer".
- Everything in Redis came back on its own (read model from Postgres, buckets refill), as the invariant requires.
