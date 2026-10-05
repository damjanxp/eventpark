# 01 — Product & Domain

This document describes **what** EventPark does and the **business rules** it must follow.
It deliberately says little about technology. That's in `02-architecture.md`.

> **ELI5:** Imagine a giant car park outside a stadium. Some people bought a parking pass online and
> show a QR code at the gate. Others just drive up, take a paper ticket and pay when they leave. The car
> park has areas (zones), some special numbered spots for VIPs, and a fixed number of spaces. EventPark is
> the "brain" that every gate asks: *"Can this car come in? Can it leave? How much does it pay?"*, and
> it shows the staff what's going on in real time.

---

## 1. Scope

### In scope
- Venues with **zones**, optional **numbered spaces** (VIP/reserved/accessible), and **gates/lanes**
  (entry or exit), each with a **gate device**.
- **Events** at a venue with time windows, per-zone capacities, pre-booking quotas and pricing.
- **Pre-booked passes** bought online (public booking site, simulated payment, QR by email/on screen).
- **Drive-up tickets** printed at the entry gate, **paid at the exit lane terminal**.
- **Entry/exit control**: validation on every scan, anti-passback, capacity enforcement, two-phase
  barrier passage, idempotent retries.
- **Staff dashboard**: live occupancy, cars inside, entries/exits per hour, revenue, available
  capacity, gate activity/health, history.
- **Operator console**: gate status, manual barrier open (audited), lost-ticket handling, ticket lookup.
- **Reports** (CSV, PDF) and **event documents** stored in S3.
- **Notifications** (booking confirmation email with QR, capacity alerts to staff).
- **Traffic simulator** to generate realistic load (10,000 vehicles, arrival waves, failures).

### Out of scope (possible future work, listed in §13)
Licence-plate recognition, offline gate mode, real Raspberry Pi deployment, multiple operator
companies (multi-tenancy), refunds, real payment providers, mobile apps.

---

## 2. Actors and roles

| Actor | How they authenticate | What they do |
|---|---|---|
| **ADMIN** | user account (JWT; later Cognito) | Everything: users, venues, zones, spaces, gates, devices, all events |
| **EVENT_MANAGER** | user account | Create/manage events, capacities, quotas, pricing; reports; documents; dashboards |
| **GATE_OPERATOR** | user account | Operator console: gate status, manual open with reason, lost tickets, void tickets |
| **PARKING_STAFF** | user account | Read-only dashboard, ticket lookup by code/plate |
| **DRIVER** | user account (self sign-up on booking site) | Browse published events, buy passes, see own bookings/QR codes |
| **Gate device** | per-device API key (not a user!) | Scan, print, authorize/complete passages, take exit payments, heartbeat |
| **Payment provider (fakepay)** | HMAC-signed webhooks | Notifies us about online payment results |

Permission matrix (✓ = allowed; "own" = only their own data):

| Capability | ADMIN | EVENT_MANAGER | GATE_OPERATOR | PARKING_STAFF | DRIVER |
|---|---|---|---|---|---|
| Manage users | ✓ | | | | |
| Manage venues/zones/spaces/gates/devices | ✓ | | | | |
| Create/edit events, pricing, quotas | ✓ | ✓ | | | |
| View dashboard & history | ✓ | ✓ | ✓ | ✓ | |
| Manual barrier open, lost ticket, void ticket | ✓ | | ✓ | | |
| Ticket lookup | ✓ | ✓ | ✓ | ✓ | |
| Request/download reports, upload documents | ✓ | ✓ | | | |
| Book passes, view bookings | | | | | own |

> ⚠️ **Common confusion:** these roles are **application roles**, enforced by our API. They are *not*
> AWS IAM roles. IAM controls what our *software components* may do inside AWS (e.g. "the reports
> worker may write to the reports bucket"). See glossary: *IAM*, *RBAC*.

---

## 3. Physical model (the venue)

```
Venue "Belgrade Arena Grounds" (timezone Europe/Belgrade)
├── Zone A  (capacity 1200)
├── Zone B  (capacity 800)
├── Zone VIP (capacity 50)  ── Spaces VIP-01 … VIP-50 (numbered)
├── Gate N1  ENTRY  ── Device n1-pi
├── Gate N2  ENTRY  ── Device n2-pi
├── Gate S1  ENTRY  ── Device s1-pi
├── Gate X1  EXIT   ── Device x1-pi (with card terminal)
└── Gate X2  EXIT   ── Device x2-pi (with card terminal)
```

- **Venue**: a physical place with a timezone.
- **Zone**: an area with a physical `total_capacity`. Most zones are just counted.
- **Space**: a numbered spot inside a zone, with a `kind` (`VIP`, `RESERVED`, `ACCESSIBLE`, `EV`).
  Only zones that sell specific spots have spaces.
- **Gate**: one lane with a direction, `ENTRY` or `EXIT`. (Real gates are often bidirectional. We keep
  one direction per lane for clarity.)
- **Device**: the controller at a gate (your Raspberry Pi). It has a QR/barcode **scanner**, a ticket
  **printer** (entry lanes), a **barrier**, a **loop detector** (senses the car), and a **card
  terminal** (exit lanes). It authenticates with its own API key, sends **heartbeats**, and is shown as
  online/offline on the dashboard.

---

## 4. Events

An **event** happens at a venue and has:

| Field | Meaning |
|---|---|
| `name`, `starts_at`, `ends_at` | the event itself (e.g. concert 20:00–23:00) |
| `gates_open_at`, `gates_close_at` | when entry is allowed (e.g. 17:00–21:30) |
| `booking_opens_at`, `booking_closes_at` | when pre-booking is possible |
| `status` | lifecycle below |
| pricing | see §8 |
| per-zone settings (`event_zones`) | see §7 |

### Event lifecycle

```
DRAFT ──publish──▶ PUBLISHED ──gates open──▶ LIVE ──gates close──▶ ENDED ──archive──▶ ARCHIVED
  │                    │
  └──────cancel────────┴──▶ CANCELLED
```

- `DRAFT`: being configured; invisible to drivers.
- `PUBLISHED`: visible on the booking site; bookings allowed inside the booking window; gates still closed.
- `LIVE`: gates accept entries and exits. Set automatically at `gates_open_at` by the maintenance job
  (or manually).
- `ENDED`: **no new entries**; exits still allowed (cars are still inside!).
- `ARCHIVED`: everything finished; unused tickets become `EXPIRED`; stats are final.
- `CANCELLED`: cancelled before going live; bookings are cancelled (refunds are out of scope).

---

## 5. Tickets

A **ticket** is the right to be in the car park for one event. There are two types:

| | **PREBOOKED** | **DRIVE_UP** |
|---|---|---|
| Created when | online booking payment succeeds | driver presses the button at an entry gate and a ticket is printed |
| Carrier | QR code (email / booking site) | printed paper ticket with a QR/barcode |
| Zone | chosen at booking | **automatically assigned** at the gate |
| Space | numbered space if VIP/reserved | none |
| Paid | upfront, fixed price | at the **exit lane terminal**, by duration |
| Capacity | guaranteed by the pre-booking quota | counted against the zone's drive-up capacity |

**Ticket code**: 16 random bytes encoded in Base32 (~26 chars), prefixed `EP1-`. It's opaque
and unguessable. It contains no data, so the gate must ask the server what it means. That's exactly
how your real system worked ("asked the server on every scan"). The QR code encodes just this string.

### Ticket state machine

```
                 entry authorized               car passes entry loop
   VALID ───────────────────────────▶ ENTERING ─────────────────────────▶ INSIDE
     ▲                                   │                                   │
     └──── authorization expired (60s) ──┘          exit authorized          │
            (DRIVE_UP: → VOID instead)          ┌──────────────────────────────┘
                                                ▼
                                             EXITING ── car passes exit loop ──▶ EXITED
                                                │
                                                └── authorization expired ──▶ INSIDE

   Any non-final state ──(operator void / lost ticket)──▶ VOID
   VALID at archive time ─────────────────────────────▶ EXPIRED
```

Rules:
- **Anti-passback:** a ticket can enter only from `VALID` and exit only from `INSIDE`. A pre-booked
  pass is **single-entry**: once `EXITED`, it cannot re-enter.
- A drive-up ticket is born in `ENTERING` (it is printed and the barrier opens in the same step). If
  the car never passes the loop (e.g. reversed away), it becomes `VOID` and the capacity is released.
- `ENTERING`/`EXITING` are **temporary holds** that expire after **60 seconds** (configurable).

---

## 6. Passages (what happens at a barrier)

A **passage** is one attempt to go through one gate. It is two-phase, because in the real world
"barrier opened" and "car actually drove through" are different moments.

```
                (exit lane, fee due)
scan ──▶ PENDING_PAYMENT ──paid──┐
  │            │                  ▼
  │            └─(5 min)─▶ EXPIRED
  └──────────────────────▶ AUTHORIZED ──(loop detector: car passed)──▶ COMPLETED
                               │
                               ├──(60s without completion)──▶ EXPIRED   (hold released)
                               └──(gate reports car reversed)──▶ ABORTED (hold released)

scan rejected ──▶ REJECTED  (stored with a reason, for analytics/audit)
```

Every gate request carries an **idempotency key** (`scan_id`, generated by the device). If the
network drops and the device retries the same scan, the server returns **the same decision** and does
not create a second passage.

### Gate flows (step by step)

**A. Entry with pre-booked QR**
1. Car arrives → loop detector → device wakes up, display: "Scan your ticket or press for ticket".
2. Driver scans QR → device sends `scan_id` + `ticket_code` to `POST /gate/v1/entry/scan`.
3. Server checks (one transaction): ticket exists → belongs to this event → event is `LIVE` →
   ticket is `VALID` (anti-passback) → (pre-booked capacity was guaranteed at booking).
4. Server sets ticket `ENTERING`, creates passage `AUTHORIZED`, writes outbox event → replies
   `OPEN` + display text ("Welcome! Zone VIP, space VIP-07").
5. Device opens barrier. Car passes the loop → device calls `POST /gate/v1/passages/{id}/complete`.
6. Server: passage `COMPLETED`, ticket `INSIDE`, records entry time. Outbox event `passage.completed`.

**B. Entry with drive-up ticket**
1. Car arrives, driver presses the ticket button → `POST /gate/v1/entry/issue` with `request_id`.
2. Server (one transaction): event `LIVE` → **automatically pick a zone** with free drive-up capacity
   (by zone priority) and atomically take one place → create ticket (`DRIVE_UP`, `ENTERING`) +
   passage `AUTHORIZED` → reply `OPEN` + print payload (ticket code, zone, entry time).
   If no zone has room → `DENY` ("Car park full").
3. Device prints ticket, opens barrier, and completes the passage as above.

**C. Exit with pre-booked QR**
1. Driver scans QR at exit → `POST /gate/v1/exit/scan`.
2. Ticket must be `INSIDE` → `EXITING`, passage `AUTHORIZED` → `OPEN`.
   (Overstay fees for pre-booked passes are out of scope.)
3. Car passes → complete → ticket `EXITED`; zone counters decremented.

**D. Exit with drive-up ticket (pay at lane)**
1. Driver scans printed ticket → `POST /gate/v1/exit/scan`.
2. Ticket `INSIDE` → server computes the fee (§8).
   - Fee is 0 (inside free minutes) or already paid (within the post-payment grace) → `OPEN` (as C).
   - Otherwise → passage `PENDING_PAYMENT` (ticket stays `INSIDE`) → reply `PAYMENT_REQUIRED`
     with `passage_id` + `amount_minor`; display "Please pay 1,200 RSD".
3. Driver taps card → device calls `POST /gate/v1/exit/pay` with `request_id`, the `passage_id`
   and a (fake) card token.
4. Server charges via fakepay's **terminal API** (synchronous, with a timeout) → `SUCCEEDED` →
   ticket marked paid and `EXITING`, passage `AUTHORIZED` → `OPEN`. `DECLINED` → display "Card declined", the
   driver may retry. Provider timeout → `PAYMENT_UNKNOWN` → device shows "Please wait" and retries
   with the **same** `request_id` (idempotency prevents a double charge).
5. Car passes → complete → `EXITED`.

**E. Rejections** (`DENY` with a machine-readable `reason` and a human `display_text`)
`UNKNOWN_TICKET`, `WRONG_EVENT`, `EVENT_NOT_LIVE`, `ALREADY_INSIDE`, `NOT_INSIDE`, `ALREADY_USED`,
`TICKET_VOID`, `ENTRY_IN_PROGRESS`, `CAR_PARK_FULL`, `DEVICE_NOT_ALLOWED` (e.g. entry scan at an
exit gate). Every rejection is stored as a `REJECTED` passage for statistics.

**F. Operator actions** (operator console, audited with user + reason)
- **Manual open**: opens a barrier remotely. The device receives the command on its next poll (or via
  the device command channel, see architecture). Creates an audit log entry and a passage marked `manual`.
- **Lost ticket**: operator finds the ticket (by plate if known, or by entry gate + time) or creates
  a lost-ticket exit; charges the **lost-ticket fee** at the lane; the ticket becomes `VOID`, the car exits.
- **Void ticket**: cancels a ticket (e.g. fraud); releases capacity if it was holding any.

---

## 7. Capacity and allocation rules

For each **event × zone** (`event_zones`) we store:

| Field | Meaning |
|---|---|
| `capacity` | spaces available for this event in this zone (≤ zone's physical capacity) |
| `prebook_quota` | how many of those are sold as pre-booked passes |
| `drive_up_capacity` | = `capacity − prebook_quota` (derived) |
| `prebook_price_minor` | price of a pass for this zone |
| `allocation_priority` | lower number = filled first by drive-up allocation (0 = disabled for drive-up) |
| counters | `prebook_held` (sold + pending payment), `drive_up_occupied` (ENTERING + INSIDE drive-ups), `prebook_inside` |

**Invariants (must hold at every instant, under any concurrency):**
1. `0 ≤ prebook_held ≤ prebook_quota`
2. `0 ≤ drive_up_occupied ≤ drive_up_capacity`
3. A numbered space is assigned to **at most one** active booking/ticket per event.
4. A ticket is never `INSIDE` twice; the number of `INSIDE` tickets in a zone equals
   `prebook_inside + drive_up_inside` (checked by the simulator's invariant checker).

**Pre-booked capacity** is reserved at booking time (a pending booking **holds** a place for
15 minutes while the driver pays; then it's released if unpaid). Pre-booked spaces stay reserved for
the whole event even after the car leaves (single-entry passes). *(Stretch: release unused pre-booked
spaces to drive-up N minutes after event start.)*

**Drive-up allocation:** go through zones by `allocation_priority`; take the first zone where
`drive_up_occupied < drive_up_capacity`, incrementing atomically. Two gates asking for the last space
at the same millisecond → exactly one gets it. **This is the central concurrency problem of the project.**

---

## 8. Pricing

Per event (`event_pricing`):

| Field | Example |
|---|---|
| `currency` | `RSD` |
| `drive_up_free_minutes` | 15 |
| `drive_up_hourly_minor` | 30000 (300.00 RSD per started hour) |
| `drive_up_daily_cap_minor` | 150000 (1,500.00 RSD max per started 24h) |
| `lost_ticket_fee_minor` | 200000 |
| `post_payment_exit_grace_minutes` | 15 |

**Drive-up fee:**
```
minutes = ceil((now − entered_at) in minutes)
if minutes ≤ free_minutes: fee = 0
else:
    days, rest = divmod(minutes, 1440)
    fee = days × daily_cap + min(ceil(rest / 60) × hourly, daily_cap)
fee_due = fee − already_paid   (if already paid and still within exit grace → 0)
```
Worked example: entered 18:05, scan at exit 21:40 → 215 min → `ceil(215/60)=4` → 4 × 300 = **1,200 RSD**.

Pre-booked price = `prebook_price_minor` of the chosen zone (fixed).

All amounts are **integers in minor units** (para). `1,200.00 RSD` = `120000`.

---

## 9. Payments

| Kind | Where | Style |
|---|---|---|
| `BOOKING` | public booking site | **asynchronous**: create payment intent → driver pays on fakepay's checkout page → fakepay sends a **signed webhook** → we confirm the booking |
| `EXIT_FEE` | exit lane card terminal | **synchronous** (with timeout): device → our API → fakepay terminal API → result |
| `LOST_TICKET` | exit lane, operator-initiated | same as `EXIT_FEE` |

Payment state machine:
```
PENDING ──▶ SUCCEEDED
   │
   ├──▶ FAILED      (declined / cancelled / expired)
   └──▶ UNKNOWN ──(reconciliation asks provider)──▶ SUCCEEDED | FAILED
```
- Every payment request to the provider has an **idempotency key**. Retrying never double-charges.
- Webhooks are verified (HMAC signature + timestamp tolerance) and processed idempotently (the
  provider may send the same webhook multiple times).
- A **reconciliation job** periodically resolves `PENDING`/`UNKNOWN` payments older than N minutes by
  asking the provider. ("Never trust that you received every webhook.")

### Booking flow
```
Driver picks event + zone (+ space for VIP)
  → POST /public/v1/bookings          (holds quota/space for 15 min, status PENDING_PAYMENT)
  → API creates payment intent at fakepay, returns checkout URL
  → driver pays on fakepay page
  → fakepay → POST /webhooks/fakepay  (signed)
  → booking CONFIRMED, ticket created (PREBOOKED, VALID), outbox: booking.confirmed
  → notifications worker: generate QR PNG → S3 → email with QR (Mailpit locally)
  → booking page shows the QR (presigned S3 URL)
Unpaid after 15 min → maintenance job → booking EXPIRED, hold released.
```

---

## 10. Dashboard, statistics and history — exact definitions

All "per hour" values are in the **venue timezone**, bucketed by hour of passage completion.

| Metric | Definition | Source |
|---|---|---|
| **Current occupancy** (per zone, total) | tickets currently `INSIDE` (+ `ENTERING` shown separately) | Redis read model (rebuildable from Postgres) |
| **Available capacity** | drive-up: `drive_up_capacity − drive_up_occupied`; pre-booked: `prebook_quota − prebook_held` | Redis read model |
| **Vehicles currently inside** | list of `INSIDE` tickets with entry time, zone, gate, type, plate if known | Postgres (paginated) |
| **Entries / exits per hour** | count of `COMPLETED` passages per direction per hour, per gate and total | `hourly_stats` (built by workers) |
| **Revenue** | sum of `SUCCEEDED` payments by kind, per hour and total | `hourly_stats` + payments |
| **Gate activity** | per gate: passages last 15 min, rejections by reason, avg decision latency, device online/offline, last heartbeat | `hourly_stats` + device heartbeats |
| **Historical statistics** | per past event: peak occupancy, total entries, revenue, avg stay duration, busiest hour, rejection rate | `event_summaries` (computed at archive) |

The dashboard updates **live** (WebSocket) for occupancy, gate activity and new passages.

---

## 11. Reports and documents

- **Reports** (`EVENT_MANAGER`/`ADMIN`): request → status `QUEUED` → reports worker generates
  **CSV** (all passages/payments) or **PDF** (event summary with charts) → uploads to S3 → status
  `READY` → download via **presigned URL** (expires in 5 min). Large events must not block the API,
  which is why this is asynchronous.
- **Event documents** (traffic plans, contracts, PDFs): uploaded **directly from the browser to S3**
  using a presigned upload; the API only stores metadata.
- **QR images** for bookings are generated by a worker and stored in S3.

---

## 12. Notifications

| Trigger | Recipient | Channel |
|---|---|---|
| Booking confirmed | driver | email with QR (Mailpit locally; SES sandbox on AWS = stretch) |
| Booking expired | driver | email |
| Zone ≥ 90% full / car park full | staff | dashboard alert (WebSocket) + log/metric |
| Gate device offline > 60 s | operators | dashboard alert + CloudWatch alarm |
| Report ready | requester | dashboard notification |

---

## 13. Non-functional requirements

| Requirement | Target |
|---|---|
| Scan decision latency (server side) | p95 < 150 ms locally, < 300 ms on AWS under rush load |
| Rush scenario | **2,000 arrivals in 15 minutes** over 6 entry lanes (+ online booking rush), zero invariant violations |
| Stress scenario | **10,000 vehicles** over an evening, with injected network failures (5% dropped responses) |
| Correctness | invariants in §7 never violated; no double charges; no double entries |
| Async lag | dashboard reflects a passage within 2 s (p95) |
| Horizontal scaling | API scales 1 → N tasks with no code change; workers scale on queue depth |
| Recoverability | Redis can be flushed at any time and rebuilt from Postgres |
| Security | passwords hashed (argon2), JWT short-lived, device keys hashed, webhooks signed, least-privilege IAM, no secrets in git |
| Observability | structured logs, metrics, alarms, dashboard; every request has a `request_id` |

---

## 14. Future work (explicitly not now)

- **Offline gate mode**: signed QR codes (Ed25519) so a Pi can validate without network, with a local
  anti-passback cache and later sync. A great follow-up once you understand the online flow.
- **Licence-plate recognition** with YOLO at gates (reuses the vehicle-detection idea).
- **Run gate-controller on a real Raspberry Pi** with real GPIO for barrier/loop.
- **AWS IoT Core** for device connectivity (MQTT, certificates) instead of HTTPS + API keys.
- **SNS fan-out** (one event → many queues), multi-tenancy, refunds, overstay fees for pre-booked passes.
