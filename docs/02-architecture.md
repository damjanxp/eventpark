# 02 — Architecture

How EventPark is built. Read `01-product-and-domain.md` first for the rules this implements.

> **ELI5:** The gate asks one question and needs an answer *right now*, like a bouncer checking a
> guest list. So the bouncer (API + database) answers immediately. Everything else, like updating the
> scoreboard, printing reports, sending emails, is written on a note and dropped in a mailbox (SQS).
> Helpers (workers) empty the mailbox at their own pace. If a helper is slow or crashes, the bouncer
> keeps working and the notes wait in the mailbox.

---

## 1. Components

```
                           ┌────────────────────────────────────────────┐
  Browsers                 │                 Frontend (React SPA)       │
  (staff, operators,  ───▶ │  /dashboard  /operator  /book              │
   drivers)                └───────────────┬────────────────────────────┘
                                           │ HTTPS (REST) + WebSocket
 Gate devices (Pi sim) ──HTTPS──┐          ▼
  N1 N2 S1 X1 X2 ...            └──▶ ┌──────────────┐   pub/sub    ┌─────────┐
                                     │  API (FastAPI)│◀────────────▶│  Redis  │ cache, read model,
 fakepay ──signed webhooks─────────▶ │  ×N instances │──────────────▶│ (Valkey)│ rate limit, pub/sub
   ▲                                 └──────┬───────┘               └────▲────┘
   └────── terminal charges / intents ──────┤ 1 transaction               │
                                            ▼                             │
                                     ┌──────────────┐                     │
                                     │  PostgreSQL  │◀──────┐             │
                                     │ (truth +     │       │             │
                                     │  outbox)     │       │             │
                                     └──────┬───────┘       │             │
                                            │ poll (SKIP LOCKED)          │
                                     ┌──────▼───────┐       │             │
                                     │ Outbox relay │       │             │
                                     └──────┬───────┘       │             │
                                            ▼               │             │
                ┌───────────────┬───────────┴───┬───────────┼───┬─────────┤
                ▼               ▼               ▼           │   ▼         │
          entry-events     exit-events    notifications  reports  maintenance   (SQS + DLQs)
                │               │               │           │   │   ▲
                ▼               ▼               ▼           │   ▼   └── scheduler tick (1/min)
          Entry worker     Exit worker   Notifications   Reports  Maintenance
          (stats, read     (stats,       worker (QR→S3,  worker   worker (expire holds,
           model, alerts)   revenue)      email)        (CSV/PDF  bookings, reconcile
                                                         → S3)    payments, event status)
                                          │                │
                                          ▼                ▼
                                         S3 (qr/, reports/, documents/)      Mailpit / SES
```

| Component | Tech | Responsibility | Scales by |
|---|---|---|---|
| **frontend** | React + TS + Vite | Staff dashboard, operator console, public booking site | static files (S3 + CloudFront) |
| **api** | FastAPI (async) | REST + WebSocket; gate hot path; auth; writes to Postgres + outbox | more ECS tasks behind ALB (CPU / request count) |
| **outbox-relay** | Python process | Publishes committed outbox rows to SQS | usually 1; safe to run 2+ (SKIP LOCKED) |
| **worker-entry / worker-exit** | Python SQS consumers | Statistics, read model, realtime push, alerts | queue backlog per task |
| **worker-notifications** | Python | QR generation → S3, emails | queue backlog |
| **worker-reports** | Python | CSV/PDF generation → S3 | queue backlog (bursty) |
| **worker-maintenance** | Python | Periodic jobs triggered by scheduler tick | 1 |
| **gate-controller** | Python (asyncio) | The "Raspberry Pi": state machine + hardware abstraction + API client | one per gate (runs on-site / on your PC) |
| **fakepay** | FastAPI | Simulated payment provider: checkout page, webhooks, terminal charges | 1 |
| **simulator** | Python (asyncio) | Spawns hundreds of virtual gate controllers + drivers; checks invariants | your laptop (or an ECS task for big runs) |
| **PostgreSQL** | postgres 17 / RDS | Source of truth | vertical (instance size) |
| **Redis** | Valkey / ElastiCache | Cache, read model, rate limiting, pub/sub | vertical |
| **SQS** | ElasticMQ / SQS | Durable queues + DLQs | managed |
| **S3** | SeaweedFS / S3 | Files | managed |

All backend processes (api, relay, workers) are **one Python package and one Docker image** with
different start commands. Same code everywhere, different entrypoint.

---

## 2. The most important rule: sync vs async

| Must be **synchronous** (caller waits) | Can be **asynchronous** (queue) |
|---|---|
| Gate decisions: scan, issue, complete, exit pay | Statistics (`hourly_stats`) |
| Booking creation (hold + payment intent) | Redis read model refresh, realtime pushes |
| Login, CRUD | QR image generation, emails |
| | Report generation, threshold alerts |
| | Expiring holds, reconciling payments (scheduled) |

**Why:** the driver is sitting in front of a barrier. The answer must come from the source of truth,
and nothing slow or flaky (S3, email, report generation) may be on that path. Async work is retried
independently and can't make the barrier slower.

### The dual-write problem and the transactional outbox

Naive code:
```python
async with db.begin():
    ticket.status = "ENTERING"
    ...
await sqs.send_message(...)   # ❌ what if this fails? DB says ENTERING, nobody gets notified.
```
Or the other order: message sent, then the DB transaction rolls back, and workers process an event that never
happened. You **cannot** atomically write to two systems.

Solution: **write the event into an `outbox` table in the same DB transaction**. A separate
**outbox relay** reads unpublished rows, sends them to SQS and marks them published.
```sql
-- relay loop (simplified)
BEGIN;
SELECT id, type, payload FROM outbox
 WHERE published_at IS NULL
 ORDER BY id
 LIMIT 100
 FOR UPDATE SKIP LOCKED;          -- multiple relays never grab the same rows
-- send batch to SQS (SendMessageBatch, max 10 per call)
UPDATE outbox SET published_at = now() WHERE id = ANY(:sent_ids);
COMMIT;
```
If the relay crashes after sending but before `UPDATE`, rows are sent **again** → that is why every
consumer must be **idempotent** (at-least-once delivery). Published rows older than 7 days are deleted
by maintenance.

---

## 3. Key flows (sequence)

### 3.1 Entry scan (pre-booked QR)
```
Device            API                         Postgres                    Outbox relay → SQS → Entry worker
  │ POST /gate/v1/entry/scan {scan_id, ticket_code}
  │──────────────▶│ auth device (API key → device → gate)
  │               │ rate-limit check (Redis token bucket per device)
  │               │ BEGIN
  │               │ SELECT passage WHERE scan_id=?  ── exists? → return stored decision (idempotent retry)
  │               │ SELECT ticket WHERE code=? FOR UPDATE
  │               │ validate: event LIVE, ticket VALID, gate is ENTRY of this venue
  │               │ UPDATE ticket SET status='ENTERING', hold_expires_at=now()+60s
  │               │ INSERT passage(AUTHORIZED, scan_id)
  │               │ INSERT outbox('passage.authorized')
  │               │ COMMIT
  │◀──────────────│ 200 {decision: OPEN, passage_id, zone, space, display_text}
  │ open barrier; loop detector sees car
  │ POST /gate/v1/passages/{id}/complete {request_id}
  │──────────────▶│ BEGIN; passage→COMPLETED; ticket→INSIDE, entered_at; counters; outbox('passage.completed'); COMMIT
  │◀──────────────│ 200
                                                     ...later (ms–seconds)...
                                                     relay publishes → entry-events
                                                     worker: upsert hourly_stats, refresh Redis zone snapshot,
                                                             PUBLISH realtime:event:{id} → API instances → WebSocket clients
```

### 3.2 Drive-up issue (automatic zone allocation), the race-critical one
```sql
-- inside the transaction; try zones in priority order, first success wins
UPDATE event_zones
   SET drive_up_occupied = drive_up_occupied + 1
 WHERE id = :zone_id
   AND drive_up_occupied < capacity - prebook_quota
RETURNING id;
-- 1 row → we got a place.  0 rows → zone full, try next zone.
```
This single statement is **atomic**: Postgres locks the row while updating, so two concurrent
requests can't both take the last place. Plus a `CHECK (drive_up_occupied <= capacity - prebook_quota)`
constraint as a safety net. In Phase 3 you'll first write the **naive** version (SELECT, compare in
Python, UPDATE) and watch the concurrency test fail. That's the point.

### 3.3 Exit with payment (synchronous provider call with timeout)
```
Device → POST /gate/v1/exit/scan  → fee due → passage PENDING_PAYMENT → {PAYMENT_REQUIRED, passage_id, amount_minor}
Device → POST /gate/v1/exit/pay {request_id, passage_id, card_token}
   API: INSERT payment(PENDING, idempotency_key=request_id)  -- commit first! (never hold a DB tx during a network call)
   API → fakepay POST /v1/terminal/charges (Idempotency-Key: request_id, timeout 5s)
        SUCCEEDED → BEGIN; payment SUCCEEDED; ticket paid + EXITING; passage AUTHORIZED; outbox; COMMIT → OPEN
        DECLINED  → payment FAILED → {DECLINED, display_text}
        timeout   → payment UNKNOWN → {PAYMENT_PENDING}; device retries same request_id;
                    API asks fakepay GET /v1/terminal/charges/{idempotency_key} to resolve
```

### 3.4 Booking + webhook
```
Browser → POST /public/v1/bookings {event_id, event_zone_id, space_id?, plate}
  API BEGIN: conditional UPDATE event_zones SET prebook_held+1 WHERE prebook_held < prebook_quota
             (+ insert booking_space with UNIQUE(event_id, space_id) for VIP)
             booking PENDING_PAYMENT expires_at=now()+15min; payment PENDING; COMMIT
  API → fakepay POST /v1/payment_intents {amount, idempotency_key=booking_id, return_url, webhook_url}
  ← {checkout_url}  → browser redirects to fakepay checkout
fakepay → POST /webhooks/fakepay  (header Fakepay-Signature: t=…,v1=HMAC_SHA256(secret, t + "." + body))
  API: verify signature + |now−t| < 5min; dedupe on provider event id;
       BEGIN; payment SUCCEEDED; booking CONFIRMED; ticket PREBOOKED/VALID; outbox('booking.confirmed'); COMMIT
  → notifications worker: QR PNG → S3 qr/{ticket_id}.png → email via SMTP
```

### 3.5 Report generation
`POST /admin/v1/events/{id}/reports {kind: CSV|PDF}` → row `QUEUED` + outbox `report.requested` → reports
worker streams rows from Postgres → writes file → `PutObject reports/{event_id}/{report_id}.csv` →
`READY` → realtime notification → `GET /admin/v1/reports/{id}/download` returns a 5-minute presigned URL.

### 3.6 Document upload (browser → S3 directly)
`POST /admin/v1/events/{id}/documents/upload-url {filename, content_type, size}` → API validates
(type/size), creates `documents` row `PENDING`, returns **presigned PUT URL** → browser uploads straight
to S3 → `POST …/documents/{doc_id}/confirm` → API `HeadObject` to verify → `READY`.
*Why:* big files never pass through (or block) our API containers.

### 3.7 Realtime path
```
worker ──PUBLISH realtime:event:{event_id} {json}──▶ Redis ──▶ every API instance (subscribed)
                                                                │ forwards to its own connected sockets
                                                                ▼
                                                     WS /ws/v1/events/{event_id}
```
With several API instances behind a load balancer, a browser is connected to **one** of them. Redis
pub/sub makes sure every instance hears every update. That's the reason this works at N > 1.

### 3.8 Scheduled maintenance
Scheduler (EventBridge Scheduler on AWS, a tiny `ticker` container locally) puts `{"type":"tick"}` into the
`maintenance` queue every minute. The maintenance worker then runs each job; each job is safe to run
concurrently/twice (conditional updates, `SKIP LOCKED`):
- expire `AUTHORIZED`/`PENDING_PAYMENT` passages past their deadline → release holds (ticket back to
  previous state, decrement counters, drive-up `ENTERING` ticket → `VOID`)
- expire `PENDING_PAYMENT` bookings → release quota/space
- event status transitions (`PUBLISHED→LIVE→ENDED`) by time
- mark devices offline (no heartbeat for 60 s) → alert
- reconcile `PENDING`/`UNKNOWN` payments older than 5 min with fakepay
- rebuild Redis read models from Postgres (self-healing)
- delete published outbox rows > 7 days, `processed_messages` > 7 days

Holds are also checked **lazily**: if a scan finds a ticket whose hold already expired, it treats it as
expired immediately, so the 1-minute tick granularity never blocks a driver.

---

## 4. Data model (PostgreSQL)

Conventions: `id uuid` primary keys (UUIDv7, time-sortable), `created_at/updated_at timestamptz`,
enums as Postgres enums or `text` + `CHECK`, money as `bigint` minor units.

### Identity
```
users(id, email UNIQUE (citext), password_hash, full_name, role, is_active, created_at)
refresh_tokens(id, user_id FK, token_hash UNIQUE, family_id, expires_at, revoked_at, created_at)
audit_log(id bigserial, actor_user_id NULL, actor_device_id NULL, action, target_type, target_id,
          reason, details jsonb, at)
```

### Venue & devices
```
venues(id, name, address, timezone)
zones(id, venue_id FK, code, name, total_capacity CHECK > 0, UNIQUE(venue_id, code))
spaces(id, zone_id FK, label, kind, UNIQUE(zone_id, label))
gates(id, venue_id FK, code, name, direction ENTRY|EXIT, has_printer bool, has_terminal bool,
      UNIQUE(venue_id, code))
devices(id, gate_id FK UNIQUE, name, api_key_hash, api_key_prefix, status ONLINE|OFFLINE|DISABLED,
        last_heartbeat_at, firmware_version, last_seen_ip, printer_status OK|PAPER_LOW|PAPER_OUT NULL)
device_commands(id, device_id FK, command OPEN_BARRIER|REBOOT|..., requested_by FK users,
                reason, created_at, delivered_at, acked_at)
```

### Events, capacity, pricing
```
events(id, venue_id FK, name, starts_at, ends_at, gates_open_at, gates_close_at,
       booking_opens_at, booking_closes_at, status, created_by FK)
event_pricing(event_id PK/FK, currency, drive_up_free_minutes, drive_up_hourly_minor,
       drive_up_daily_cap_minor, lost_ticket_fee_minor, post_payment_exit_grace_minutes)
event_zones(id, event_id FK, zone_id FK, capacity, prebook_quota, prebook_price_minor,
       allocation_priority, prebook_held, prebook_inside, drive_up_occupied,
       UNIQUE(event_id, zone_id),
       CHECK (prebook_quota <= capacity),
       CHECK (prebook_held BETWEEN 0 AND prebook_quota),
       CHECK (drive_up_occupied BETWEEN 0 AND capacity - prebook_quota))
```

### Bookings, tickets, passages, payments
```
vehicles(id, plate_normalized UNIQUE, plate_display, owner_user_id NULL)
bookings(id, user_id FK, event_id FK, event_zone_id FK, space_id NULL FK, vehicle_id NULL FK,
         status PENDING_PAYMENT|CONFIRMED|EXPIRED|CANCELLED, price_minor, currency,
         expires_at, created_at)
  -- one active booking per space per event (VIP double-booking impossible):
  UNIQUE INDEX ON bookings(event_id, space_id) WHERE space_id IS NOT NULL
                                                AND status IN ('PENDING_PAYMENT','CONFIRMED')
tickets(id, event_id FK, type PREBOOKED|DRIVE_UP, code UNIQUE, booking_id NULL UNIQUE FK,
        event_zone_id FK, space_id NULL, vehicle_id NULL,
        status VALID|ENTERING|INSIDE|EXITING|EXITED|VOID|EXPIRED,
        hold_expires_at NULL, entered_at NULL, exited_at NULL,
        paid_minor bigint DEFAULT 0, paid_at NULL,
        issued_at, issued_by_device_id NULL, qr_s3_key NULL, version int)
  INDEX ON tickets(event_id, status)
passages(id, event_id FK, gate_id FK, device_id FK, ticket_id NULL FK, direction ENTRY|EXIT,
         status PENDING_PAYMENT|AUTHORIZED|COMPLETED|EXPIRED|ABORTED|REJECTED,
         reject_reason NULL, amount_due_minor NULL, idempotency_key UNIQUE (scan_id/request_id),
         response jsonb (stored decision, returned on retries), manual bool, operator_user_id NULL,
         authorized_at, deadline_at, completed_at, decision_ms int)
  INDEX ON passages(event_id, completed_at)
  INDEX ON passages(status, deadline_at) WHERE status IN ('AUTHORIZED','PENDING_PAYMENT')
payments(id, kind BOOKING|EXIT_FEE|LOST_TICKET, booking_id NULL, ticket_id NULL, passage_id NULL,
         amount_minor, currency, status PENDING|SUCCEEDED|FAILED|UNKNOWN|REFUNDED,
         idempotency_key UNIQUE, provider_ref NULL, failure_reason NULL, created_at, updated_at)
webhook_events(provider_event_id PK, received_at, payload jsonb)   -- webhook dedupe
```

### Messaging & analytics
```
outbox(id bigserial, message_id uuid UNIQUE, type, routing_key, payload jsonb,
       created_at, published_at NULL)
  INDEX ON outbox(id) WHERE published_at IS NULL
processed_messages(consumer, message_id, processed_at, PRIMARY KEY(consumer, message_id))
hourly_stats(event_id, gate_id, hour_start timestamptz, entries, exits, rejections,
             revenue_minor, decision_ms_sum, decision_count,
             PRIMARY KEY(event_id, gate_id, hour_start))
event_summaries(event_id PK, peak_occupancy, peak_at, total_entries, total_exits, revenue_minor,
                avg_stay_minutes, busiest_hour, rejection_rate, computed_at)
reports(id, event_id, kind CSV|PDF, status QUEUED|RUNNING|READY|FAILED, s3_key, requested_by,
        error, created_at, completed_at)
documents(id, event_id, filename, content_type, size_bytes, s3_key, status PENDING|READY,
          uploaded_by, created_at)
```

> **Why counters on `event_zones` instead of `COUNT(*)` every time?** Counting thousands of tickets
> on every scan under a rush is slow and still racy. A counter row + conditional update is O(1) and
> atomic. The cost: counters can drift if code is buggy → the maintenance job and the simulator's
> invariant checker compare counters with real counts.

---

## 5. Concurrency & correctness toolbox

| Invariant | Mechanism |
|---|---|
| Zone drive-up capacity never exceeded | conditional `UPDATE … WHERE occupied < cap RETURNING` + `CHECK` |
| Pre-book quota never exceeded | same, on `prebook_held` |
| VIP space never double-booked | partial `UNIQUE` index on active bookings |
| Ticket can't enter twice | `SELECT … FOR UPDATE` on the ticket row + state machine check |
| Retried scan isn't processed twice | `UNIQUE(idempotency_key)` on passages; return stored `response` |
| No double charge | `UNIQUE(idempotency_key)` on payments + provider idempotency key |
| Message processed once (effectively) | `processed_messages` insert in the same tx as the effect; conflict → skip |
| Relay doesn't double-grab rows | `FOR UPDATE SKIP LOCKED` |

Transaction isolation: **READ COMMITTED** (Postgres default) + explicit row locks/conditional updates.
In Phase 3 you'll also try **SERIALIZABLE** and see retries (`40001 serialization_failure`) to understand the
trade-off.

Lock ordering rule (avoid deadlocks): when one transaction locks several rows, always lock in the order
**event_zone → ticket → passage → payment**.

---

## 6. Messaging details

**Queues** (each with a DLQ `<name>-dlq`, `maxReceiveCount = 5`):

| Queue | Message types | Consumer | Visibility timeout |
|---|---|---|---|
| `entry-events` | `passage.authorized`, `passage.completed`, `passage.expired`, `passage.rejected` (entry gates) | worker-entry | 30 s |
| `exit-events` | same for exit gates, `payment.succeeded` (EXIT_FEE/LOST_TICKET) | worker-exit | 30 s |
| `notifications` | `booking.confirmed`, `booking.expired`, `alert.zone_threshold`, `alert.device_offline`, `report.ready` | worker-notifications | 60 s |
| `reports` | `report.requested` | worker-reports | 300 s (+ heartbeat extend) |
| `maintenance` | `tick` | worker-maintenance | 60 s |

**Envelope** (every message):
```json
{
  "message_id": "0192…uuid",
  "type": "passage.completed",
  "version": 1,
  "occurred_at": "2026-10-04T18:05:12.345Z",
  "event_id": "…",
  "data": { "passage_id": "…", "ticket_id": "…", "gate_id": "…", "direction": "ENTRY", "zone_id": "…" },
  "meta": { "request_id": "…", "producer": "api" }
}
```

**Consumer loop:** long-poll `ReceiveMessage(WaitTimeSeconds=20, MaxNumberOfMessages=10)` → for each:
`BEGIN; INSERT processed_messages … ON CONFLICT DO NOTHING` → if 0 rows: already done, delete & skip →
else do the DB effects → `COMMIT` → non-DB effects that are **idempotent by nature** (SET absolute values
in Redis, `PUBLISH`, `PutObject` with deterministic key) → `DeleteMessage`. On exception: don't delete;
SQS redelivers after the visibility timeout; after 5 failures → DLQ → alarm.

**Ordering:** standard SQS queues don't guarantee order. Our consumers don't need order because they
**read current state from Postgres** or set absolute values, instead of applying deltas in sequence.
(FIFO queues are discussed in `decisions.md`.)

**Graceful shutdown:** on SIGTERM (ECS stopping a task) a worker stops polling, finishes in-flight
messages, then exits. ECS gives 30 s by default (`stopTimeout`).

---

## 7. Redis usage

| Use | Keys | Pattern |
|---|---|---|
| **Cache-aside** for event config (event, zones, pricing, gate→venue map) | `cache:event:{id}` (TTL 60 s) | read: try Redis → miss → Postgres → SET with TTL; write path: update Postgres then `DEL` key |
| **Occupancy read model** | `occ:event:{id}` hash: per zone `capacity, drive_up_occupied, prebook_held, inside…` | workers `HSET` absolute values from Postgres; dashboard & booking site read it |
| **Rate limiting** | `rl:device:{id}`, `rl:ip:{ip}:{route}`, `rl:login:{email}` | token bucket in a Lua script (atomic) |
| **Realtime fan-out** | channel `realtime:event:{id}` | `PUBLISH` by workers; every API instance `SUBSCRIBE`s |
| **WebSocket ticket** | `wsticket:{random}` (TTL 30 s) | short-lived one-time token to open a WebSocket (browsers can't set auth headers on WS) |

**Redis is never required for correctness.** If Redis is down: cache misses go to Postgres, rate
limiting fails open (logged + metric), the dashboard falls back to polling the API. Test this in Phase 7
by stopping the Redis container during a simulation.

*Experiment (Phase 9):* move drive-up admission into Redis (atomic Lua `if occupied < cap then INCR`)
with Postgres reconciliation, measure the latency difference, and write down why we **didn't** keep it
as the source of truth.

---

## 8. API design

Conventions:
- Prefixes: `/admin/v1` (staff, JWT), `/public/v1` (drivers + anonymous), `/gate/v1` (devices, API key),
  `/webhooks` (providers, signatures), `/ws/v1` (WebSockets), `/healthz` `/readyz`.
- Errors: **RFC 9457 problem+json** `{type, title, status, detail, code, request_id}`.
- Pagination: cursor-based (`?cursor=…&limit=50`) for lists that grow (passages, tickets).
- Every response has header `X-Request-ID` (taken from the request or generated).
- OpenAPI is generated by FastAPI and used to generate the TypeScript client.

### Endpoints (initial list; the OpenAPI is the final truth)

**Auth** `POST /admin/v1/auth/login`, `POST /admin/v1/auth/refresh`, `POST /admin/v1/auth/logout`,
`GET /admin/v1/me`; drivers: `POST /public/v1/auth/register`, `POST /public/v1/auth/login` (same mechanism, role DRIVER).

**Admin – setup** CRUD: `/admin/v1/users`, `/venues`, `/venues/{id}/zones`, `/zones/{id}/spaces`,
`/venues/{id}/gates`, `/gates/{id}/device` (`POST` creates device and returns the API key **once**,
`POST …/rotate-key`).

**Admin – events** CRUD `/admin/v1/events`, `PUT /events/{id}/pricing`, `PUT /events/{id}/zones`,
`POST /events/{id}/publish|go-live|end|archive|cancel`.

**Admin – operations** `GET /events/{id}/occupancy`, `GET /events/{id}/inside?cursor=`,
`GET /events/{id}/stats/hourly`, `GET /events/{id}/gates/activity`, `GET /events/{id}/passages?…`,
`GET /tickets/lookup?code=|plate=`, `POST /tickets/{id}/void`, `POST /gates/{id}/manual-open {reason}`,
`POST /tickets/lost {event_id, gate_id, plate?}`, `GET /history/events` (summaries).

**Admin – files** `POST /events/{id}/reports`, `GET /reports/{id}`, `GET /reports/{id}/download`,
`POST /events/{id}/documents/upload-url`, `POST /documents/{id}/confirm`, `GET /events/{id}/documents`.

**Public** `GET /public/v1/events` (published), `GET /public/v1/events/{id}` (+ availability from Redis),
`GET /public/v1/events/{id}/zones/{zid}/spaces` (free VIP spaces), `POST /public/v1/bookings`,
`GET /public/v1/bookings` (own), `GET /public/v1/bookings/{id}` (+ QR presigned URL when ready).

**Gate (device)** — header `Authorization: Device <api_key>`
`POST /gate/v1/entry/scan`, `POST /gate/v1/entry/issue`, `POST /gate/v1/exit/scan`, `POST /gate/v1/exit/pay`,
`POST /gate/v1/passages/{id}/complete`, `POST /gate/v1/passages/{id}/abort`,
`POST /gate/v1/heartbeat` (response includes pending `commands`), `POST /gate/v1/commands/{id}/ack`.

Gate decision response (all gate endpoints use one shape):
```json
{ "decision": "OPEN | DENY | PAYMENT_REQUIRED | PAYMENT_PENDING | DECLINED",
  "reason": "CAR_PARK_FULL", "passage_id": "…", "amount_minor": 120000, "currency": "RSD",
  "zone": "A", "space": null, "display_text": "Car park full – please follow signs",
  "print": { "ticket_code": "EP1-…", "zone": "A", "entered_at": "…" } }
```

**Webhooks** `POST /webhooks/fakepay`. **WebSocket** `GET /ws/v1/events/{id}?ticket=…`
(obtain the ticket via `POST /admin/v1/ws-ticket`).

---

## 9. Authentication & authorization

- **Passwords:** argon2id (`argon2-cffi`). Never stored or logged in plain text.
- **Access token:** JWT (HS256 locally; key from SSM on AWS), 15 min, claims `sub`, `role`, `exp`, `iat`, `jti`.
  Sent as `Authorization: Bearer`. Kept **in memory** in the SPA (not localStorage → XSS can't read it after reload).
- **Refresh token:** random 256-bit, stored **hashed** in `refresh_tokens`, 7 days, sent as an `HttpOnly; Secure;
  SameSite=Strict` cookie scoped to `/admin/v1/auth` (and the public equivalent). **Rotation**: each refresh issues a new one and
  revokes the old; reuse of a revoked token revokes the whole family (theft detection).
- **RBAC:** a FastAPI dependency `require_roles(Role.ADMIN, Role.EVENT_MANAGER)` per route. Drivers can only
  see their own bookings (ownership check in the service, not just the role).
- **Devices:** API key `epd_<prefix>_<secret>`; DB stores prefix + argon2 hash; lookup by prefix, verify hash; cache
  the verified device in Redis for 60 s. Disabled device → 401.
- **Webhooks:** HMAC-SHA256 over `timestamp.body` with a shared secret; reject if timestamp older than 5 min
  (replay protection); compare with `hmac.compare_digest`.
- **Phase 15:** replace user auth with **Amazon Cognito** (user pool, groups = roles, API verifies Cognito JWTs via
  JWKS). Device and webhook auth stay as they are.

---

## 10. Gate controller (the Raspberry Pi)

```
gate-controller/
  src/gatectl/
    hal/            # Hardware Abstraction Layer: interfaces + implementations
      base.py       #   Scanner, Printer, Barrier, LoopDetector, CardTerminal, Display (Protocols)
      simulated.py  #   in-memory fakes driven by the simulator / keyboard
      # rpi.py      #   (future) GPIO + USB scanner + ESC/POS printer
    client.py       # API client: timeouts, retries with exponential backoff + jitter, idempotency keys
    machine.py      # the lane state machine
    heartbeat.py    # every 5s: status, queue sizes, firmware → receives commands
    main.py         # config from env, wiring, run
```

Lane state machine (entry lane):
```
IDLE ─car on loop─▶ WAITING_FOR_INPUT ─scan/button─▶ REQUESTING ─OPEN─▶ BARRIER_OPEN ─car passed─▶ COMPLETING ─▶ IDLE
                         │                               │                    │
                         └─car left─▶ IDLE              DENY─▶ SHOW_MESSAGE    └─timeout 30s, no car─▶ ABORTING ─▶ IDLE
```
Drive-up issue adds `REQUESTING ─OPEN─▶ PRINTING ─ticket taken─▶ BARRIER_OPEN`: the barrier opens only when the
driver pulls the ticket out of the slot. Scanner and button input is ignored outside `WAITING_FOR_INPUT` (no car on
the arming loop, no ticket). Exit lane adds `AWAITING_PAYMENT ─card─▶ CHARGING`.

Rules the device follows:
- Generates a **UUID per scan/press/payment** and reuses it for every retry of that action.
- Request timeout 2 s; up to 3 retries with backoff `0.2s·2^n ± jitter`; then shows "Please call
  operator" and keeps the action ID so a later retry is still safe.
- Never opens the barrier without an `OPEN` decision (fail closed), except a manual-open command.
- Heartbeat every 5 s, including printer status; the server marks the device offline after 60 s of silence.
- With `PAPER_OUT` the ticket button shows "No tickets, please use another lane" without asking the server.

---

## 11. fakepay (simulated payment provider)

A small FastAPI app that behaves like Stripe-lite:
- `POST /v1/payment_intents` → `{id, status: requires_payment, checkout_url}`; idempotent by `Idempotency-Key`.
- `GET /checkout/{id}`: a tiny HTML page with "Pay" / "Decline" buttons (and auto mode for the simulator).
- Sends **webhooks** `payment_intent.succeeded|failed` signed with HMAC, **retries** with backoff if our API
  returns non-2xx, may send **duplicates** (configurable) to prove our idempotency.
- `POST /v1/terminal/charges` → synchronous result after a random 0.3–2 s delay; card tokens:
  `tok_ok` → success, `tok_decline` → declined, `tok_slow` → 8 s (forces our timeout), `tok_random` → configurable failure rate.
- `GET /v1/terminal/charges/{idempotency_key}`, `GET /v1/payment_intents/{id}` for reconciliation.
- Config: failure rate, latency range, duplicate-webhook probability, webhook delay.

---

## 12. Traffic simulator

```
simulator/
  scenarios/*.yaml       # arrival curves, mix, failures
  src/simulator/
    world.py             # venue/event setup via admin API (or seed script)
    drivers.py           # virtual drivers: arrive, choose gate (shortest queue), scan/press, park, leave
    gates.py             # N in-process gate controllers using gatectl with simulated HAL
    booking_rush.py      # concurrent online bookings (presale scenario)
    chaos.py             # drop responses, add latency, kill a worker, flush Redis
    invariants.py        # after (and during) the run: compare DB state vs rules
    report.py            # latency percentiles per endpoint, decisions, throughput → markdown/CSV
```
Scenario example:
```yaml
name: rush-2000-in-15
event: { zones: {A: 1200, B: 800, VIP: 50}, prebook_quota: {A: 300, B: 200, VIP: 50} }
entry_gates: 6
exit_gates: 3
arrivals:
  - { from: "-00:15", to: "00:00", vehicles: 2000, distribution: poisson }
mix: { prebooked: 0.3, drive_up: 0.7 }
stay_minutes: { distribution: normal, mean: 180, stddev: 30 }
payments: { decline_rate: 0.05, slow_rate: 0.02 }
chaos: { drop_response_rate: 0.05 }
time_scale: 60     # 1 simulated minute = 1 real second
```
The simulator uses a **time-scaled clock** for drivers. For the server, fees and holds depend on server time, so
fee checks in the invariant checker use the server's recorded timestamps.

---

## 13. Frontend

- One SPA, three areas: `/dashboard/*` (staff), `/operator/*` (gate operators), `/book/*` (drivers).
  Route guards by role.
- Data: TanStack Query for REST; a `useEventStream(eventId)` hook for the WebSocket that **patches the
  Query cache** (so components don't care whether data came from REST or WS). Reconnect with backoff;
  fall back to polling every 5 s if WS is down.
- Typed API client generated from OpenAPI (`npm run gen:api`).
- Dashboard widgets: occupancy per zone (bars + numbers), cars inside (table), entries/exits per hour (line
  chart), revenue (KPI + chart), gate activity (grid of gate cards with online status, last 15 min counts,
  rejections), event history (table + detail).
- Operator console: gate cards with **Manual open** (reason required), ticket lookup, lost ticket,
  live passage feed.
- Booking site: event list → event page with zone availability (live) → choose zone / VIP space → plate →
  pay (redirect to fakepay) → booking page with QR.
- Locally Vite proxies `/api` and `/ws` to the backend, so it's **same-origin** (like CloudFront later). No CORS needed.

---

## 14. Configuration (12-factor)

All config comes from environment variables, read by `pydantic-settings`:

| Variable | Local value | AWS value |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://…@postgres:5432/eventpark` | RDS endpoint (password from SSM) |
| `REDIS_URL` | `redis://valkey:6379/0` | ElastiCache endpoint (`rediss://` TLS) |
| `AWS_REGION` | `eu-central-1` | `eu-central-1` |
| `AWS_ENDPOINT_URL_SQS` | `http://elasticmq:9324` | *(unset → real AWS)* |
| `AWS_ENDPOINT_URL_S3` | `http://seaweedfs:8333` | *(unset)* |
| `S3_PUBLIC_ENDPOINT_URL` | `http://localhost:8333` (browser-reachable host for presigned URLs) | *(unset)* |
| `AWS_ACCESS_KEY_ID/SECRET` | dummy values for emulators | *(unset → ECS task role credentials)* |
| `QUEUE_URL_*` / `BUCKET_*` | local names | from Terraform outputs |
| `JWT_SECRET`, `FAKEPAY_WEBHOOK_SECRET` | `.env` | SSM SecureString |
| `FAKEPAY_URL` | `http://fakepay:8000` | Service Connect name `http://fakepay:8000` |
| `SMTP_URL` | `smtp://mailpit:1025` | SES SMTP (stretch) or disabled |

> **Gotcha you will hit:** a presigned URL's signature includes the **host**. If the API signs with
> `http://seaweedfs:8333` (Docker-internal name), your browser can't open it. That's why there is a separate
> public endpoint for presigning locally.

---

## 15. Observability (application side)

- **Logs:** structlog JSON to stdout: `ts, level, msg, service, request_id, user_id/device_id, event_id,
  route, status, duration_ms`. ECS ships stdout to CloudWatch Logs.
- **Request ID:** middleware reads/creates `X-Request-ID`, puts it in the log context and in outbox message
  `meta`, and workers log it again. One ID lets you follow a scan from gate → API → queue → worker.
- **Metrics:** CloudWatch **Embedded Metric Format** (EMF): a structured log line that CloudWatch turns into
  metrics, so no agent is needed. Metrics: `GateDecisionLatency` (by decision), `GateDecisions` (by decision/reason),
  `CapacityRejections`, `PaymentsByStatus`, `OutboxLag` (age of oldest unpublished row), `WorkerProcessingTime`,
  `RateLimited`, `RedisFallbacks`. Locally they are just log lines.
- **Health:** `/healthz` = process alive (no dependencies). `/readyz` = can reach Postgres + Redis.
  (Why two? A load balancer should stop sending traffic to a task that can't reach the DB, but ECS shouldn't
  *kill* every task just because the DB had a blip.)

---

## 16. Testing strategy

| Level | What | Tools |
|---|---|---|
| Unit | `domain/`: pricing, state machines, allocation choice, signature verification | pytest (pure, fast) |
| Integration | services + real Postgres/Redis; S3/SQS via moto | testcontainers, moto |
| Concurrency | 200 parallel issue requests for the last 10 spaces → exactly 10 OPEN; double scan → one entry; duplicate webhooks → one confirmation | pytest-asyncio + asyncio.gather |
| Contract | gate API responses match the decision schema; OpenAPI diff check in CI | schemathesis (optional) |
| End-to-end | docker compose up → simulator "smoke" scenario (200 cars) → invariants pass | CI job |
| Frontend | components + hooks | vitest, React Testing Library; 1–2 Playwright flows |

---

## 17. Local stack (docker compose)

| Service | Image / build | Ports (host) |
|---|---|---|
| postgres | `postgres:17` | 5432 |
| valkey | `valkey/valkey:8` | 6379 |
| elasticmq | `softwaremill/elasticmq-native` (queues + DLQs from `elasticmq.conf`) | 9324, 9325 (UI) |
| seaweedfs | `chrislusf/seaweedfs` (`server -s3`) | 8333 |
| mailpit | `axllent/mailpit` | 8025 (UI), 1025 (SMTP) |
| api | `./backend` (uvicorn, reload) | 8000 |
| outbox-relay, worker-* | `./backend` (same image, different command) | – |
| ticker | `./backend` (`python -m eventpark.ticker`) | – |
| fakepay | `./fakepay` | 8100 |
| frontend | `npm run dev` on host (or compose profile) | 5173 |
| gate controllers / simulator | run on host against `localhost:8000` | – |

An `init` one-shot service runs `alembic upgrade head` and creates buckets/queues (idempotently) before the
app services start (`depends_on: condition: service_completed_successfully`).

> Emulator note: the local-AWS landscape changed in 2025–26 (MinIO's community images stopped; LocalStack
> Community now needs an account token). We use ElasticMQ + SeaweedFS; if SeaweedFS gives trouble, swap
> to `adobe/s3mock`. Tests don't depend on either: they use **moto**. See `decisions.md`.
