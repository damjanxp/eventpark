# Experiments: the gate controller (Phase 4)

Run a lane by hand: `make seed` (once), then `make lane GATE=N1` (entry) or `make lane GATE=X1` (exit).

## 1. API down while a driver is at the lane (2026-11-04)

Script: car arrives (`c`) → `docker compose stop api` → driver presses the ticket button (`b`) → API restarted →
driver presses again (`b`) → takes the ticket (`t`) → drives through (`p`).

```
DISPLAY: Scan your ticket or press for a ticket
DISPLAY: One moment...
DISPLAY: Please call staff (button on the left)      <- 4 attempts (1 + 3 retries, backoff 0.2/0.4/0.8 s +- jitter), then fail closed
DISPLAY: One moment...                               <- API is back, driver presses again: SAME request_id is sent
DISPLAY: Please take your ticket - Zone A
DISPLAY: Have a nice day!
```

Passages at that gate afterwards: **1, COMPLETED**. The first press never reached the server (connection refused),
the second carried the same idempotency key, so even if the first had been processed and only its response lost, the
server would have returned the stored answer instead of taking a second place.

**What we learned on the way:** the first attempt at this experiment failed for a different reason. Restarting the
`api` container re-ran the one-shot `init` service (it's a dependency), and `init`'s image had been built *before* the
Phase 3 migration file existed. Alembic found the database at a revision the image didn't know
("Can't locate revision 84e2e5d03d60") and exited 255, so the API never started. Fix: in Compose, `init` now mounts
`./backend/migrations` (like `api` mounts `./backend/src`), so local migrations can't go stale. On AWS this can't
happen: CI builds one image that contains both the code and its migrations, and the migration task runs that image.

## 2. Barrier open, no car (timeout → abort)

Covered by `tests/test_machine.py::test_barrier_open_but_no_car_times_out_and_aborts`: after 30 s the lane calls
`POST /passages/{id}/abort`, closes the barrier and returns to IDLE; the server releases the hold (a drive-up ticket
becomes VOID and its place goes back to the pool).

## 3. Add 1.5 s artificial latency

Not run yet. With the 2 s client timeout, 1.5 s still succeeds on the first attempt; the driver waits ~1.5 s with
"One moment..." on the screen. At ~2 s the client starts retrying, so a server that is *slow* (not down) gets up to 4x
the load from every lane: the reason retries need backoff + jitter, and the reason the server's p95 target is
150 ms, far below the client timeout.
