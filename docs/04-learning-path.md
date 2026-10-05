# 04 — Learning Path

This is the plan you follow. 16 phases take you from an empty folder to a monitored, auto-scaling
system on AWS. Each phase teaches the concepts it needs **at the moment you need them**.

## How every phase works

1. **Read** the phase's *Concepts*. Each has an **ELI5** (the intuition), a **Real explanation** (the
   precise version you'd give in an interview) and **In EventPark** (why we need it here).
2. **Write first.** Answer the *Journal prompt* in `docs/journal/phase-NN.md` in your own words,
   before any code. The agent reviews it and corrects you. (That's how you learn best.)
3. **Build** the *Tasks* with the agent. Tasks marked **🧑‍💻 YOU WRITE** are yours to code; the
   agent gives hints, reviews and writes tests, but not the solution.
4. **Experiment** where the phase says so. Break things on purpose; that's where understanding comes from.
5. **Checkpoint:** answer the questions out loud or in the journal. If you can't answer one, you're not done.
6. Tick the phase in `progress.md`. Commit.

## Timeline (full-time)

| Week | Phases | Milestone |
|---|---|---|
| 1 | 0 → 3 | Local API makes correct gate decisions under concurrency |
| 2 | 4 → 7 | Gates, payments, async workers, Redis, realtime all work locally |
| 3 | 8 → 10 | Dashboard + simulator; first things exist in AWS |
| 4 | 11 → 13 | Whole system runs on AWS, deployed by CI/CD |
| 5–6 | 14 → 15 | Monitoring, scaling experiments, Cognito, portfolio polish |

Realistic total: **≈ 28 working days**. To fit a strict 4 weeks, use the **cut list**:
- Phase 8: dashboard + operator console only; booking site = one plain page (no VIP space picker).
- Phase 6: reports as CSV only (no PDF); skip event documents upload.
- Phase 5: skip lost-ticket flow.
- Phase 15: skip Cognito (keep your own JWT auth); do the security review + README only.
Never cut: Phase 3 concurrency work, the outbox, Terraform, CI/CD, alarms. They're the heart of the project.

---

## Phase 0 — Foundations & a safe AWS account (≈ 1 day)

**Goal:** tools installed, AWS account safe from surprise bills, you understand the big picture.

### Concepts

**Cloud computing**
- *ELI5:* Instead of buying computers, you rent them (and ready-made services like databases) by the hour from a huge provider.
- *Real:* On-demand, metered access to compute, storage and managed services over an API. Models: **IaaS**
  (raw VMs/networks, e.g. EC2), **PaaS/managed services** (RDS, SQS: the provider runs the software), **serverless**
  (Fargate, Lambda: no servers to manage at all). You trade capital cost and ops work for usage-based cost.
- *In EventPark:* we use managed services everywhere so we focus on the architecture, not on patching servers.

**Regions and Availability Zones (AZs)**
- *ELI5:* A region is a city with AWS data centres; an AZ is one building (or group) in that city, with its own power and network.
- *Real:* Regions are isolated geographic areas (eu-central-1 = Frankfurt). Each has ≥3 AZs, physically separate
  but connected by low-latency links. Spreading across AZs survives a data-centre failure.
- *In EventPark:* subnets in 2 AZs (the ALB requires 2). Single-AZ database to save money (we say so honestly).

**Shared responsibility**
- *ELI5:* AWS locks the building; you lock your own room.
- *Real:* AWS secures the infrastructure ("security **of** the cloud"); you secure configuration, data, identities,
  network rules and code ("security **in** the cloud").

**Root user, IAM identities, MFA, billing**
- *ELI5:* The root user is the master key to the whole account. You put it in a safe and use a personal key for daily work.
- *Real:* Root can do everything incl. closing the account and changing billing; protect with MFA and don't use
  it. Daily work via an IAM user with MFA (we can't use IAM Identity Center on the Free plan, see ADR-022). AWS bills per second/hour/request; credits
  offset bills; **budgets** alert you, they don't stop spending.

### Tasks
- [ ] Install: Docker Desktop (WSL2 backend), Git, **uv** (Python 3.13), **Node 24 LTS**, **Terraform ≥ 1.11**,
      **AWS CLI v2**, VS Code (+ Python, Ruff, Docker, HashiCorp Terraform, ESLint extensions).
- [ ] Decide where the repo lives. Docker bind mounts and file watching are **much faster from the WSL2
      filesystem** (`~/projects/eventpark`) than from `C:\…`. Recommended: work inside WSL2. (If you keep
      `C:\Projekti\AWS`, expect slower hot-reload.)
- [ ] Create the AWS account on the **Free plan** (never upgrade, ADR-022); root MFA; IAM user + MFA, no access keys;
      `aws login`; `aws sts get-caller-identity` works.
- [ ] Budgets ($25 monthly, $5 daily, alerts at 20/50/80/100%), Cost Anomaly Detection, Cost Explorer. Region eu-central-1.
- [ ] Create the GitHub repo `eventpark` (public, for the portfolio) and push the docs.
- [ ] Read `01`, `02` (skim), `decisions.md`.

### Journal prompt: your real-world expertise (important!)
Write `journal/phase-00.md`: **describe the real gate system you've worked on at your firm**. What happens
step by step when a car arrives, from loop detector to barrier closing? What does the Pi do, what does the
server do? What happens when the network drops? How do the printer and the paid-ticket exit work? What goes
wrong in practice (paper jams, cars reversing, tailgating, people losing tickets)? The agent will compare it
with `01-product-and-domain.md` and you'll adjust the spec where reality differs. **You are the domain expert here.**

### Checkpoint
1. What's the difference between a region and an AZ, and why does the ALB need two AZs?
2. Why should you never use the root user day-to-day?
3. Does a budget stop AWS from charging you? What does?
4. In one minute, explain the EventPark architecture from gate scan to dashboard update.

**Done when:** tools work, `aws sts get-caller-identity` prints your identity, budgets exist, journal written.

---

## Phase 1 — Local walking skeleton + CI (≈ 1.5 days)

**Goal:** `docker compose up` starts the whole local infrastructure and a FastAPI app with health checks,
logging, config and migrations, and GitHub runs lint + tests on every push.

### Concepts

**Containers and images**
- *ELI5:* An image is a frozen lunchbox with your app and everything it needs; a container is that lunchbox opened and running. It runs the same on any machine.
- *Real:* An image is a stack of read-only filesystem **layers** + metadata (entrypoint, env). A container is a
  process isolated with Linux namespaces/cgroups, with a writable layer on top. Layers are cached: order Dockerfile
  steps from least- to most-frequently changing (dependencies before source code). **Multi-stage builds** keep build
  tools out of the final image. Run as a **non-root** user.
- *In EventPark:* one backend image, many roles (api, relay, workers) via different commands. The same image runs on ECS.

**Docker Compose networking and volumes**
- *ELI5:* Compose puts all your containers on one private network where they call each other by name, like extensions in an office phone system.
- *Real:* Compose creates a bridge network with built-in DNS; service names resolve to container IPs. `ports:`
  publishes to your host. Named **volumes** persist data across restarts; `docker compose down -v` deletes them.
- *In EventPark:* the API reaches `postgres:5432`; your browser reaches `localhost:8000`. Two different worlds.

**12-factor configuration**
- *ELI5:* The app reads settings from labels stuck on the outside of the box, so you can ship the same box everywhere and just change the labels.
- *Real:* Config (URLs, credentials, flags) lives in environment variables, never in code. Validated at startup
  (`pydantic-settings`) so a missing variable fails fast.

**Liveness vs readiness**
- *ELI5:* "Are you awake?" vs "Are you ready to work?"
- *Real:* Liveness (`/healthz`) checks only the process; failure → restart. Readiness (`/readyz`) checks dependencies;
  failure → stop routing traffic, don't restart. Mixing them causes restart storms when the DB blips.

**Database migrations**
- *ELI5:* A numbered list of changes to the database layout, so every copy of the database can be brought to the same shape.
- *Real:* Alembic revisions are versioned scripts (`upgrade`/`downgrade`); the DB stores its current revision.
  Autogenerate compares models to the DB but **you must review** the output.

**Continuous Integration**
- *ELI5:* A robot that checks your homework every time you hand it in.
- *Real:* On every push/PR, a fresh runner installs dependencies, lints, type-checks and tests. A red build blocks merging.

### Tasks
- [ ] Monorepo skeleton (see `CLAUDE.md` §5), `.gitignore`, `.env.example`, `.editorconfig`, `README.md` stub.
- [ ] `backend/` with uv: FastAPI app factory, `core/config.py` (pydantic-settings), `/healthz`, `/readyz`
      (checks Postgres + Redis), structlog JSON logging.
- [ ] 🧑‍💻 **YOU WRITE:** the **request-ID middleware** (read `X-Request-ID` or generate one, bind it into the log
      context, return it in the response header).
- [ ] 🧑‍💻 **YOU WRITE:** the backend **Dockerfile**: multi-stage (uv install → slim runtime), non-root user,
      `HEALTHCHECK`, sensible layer order. Agent reviews size and layer caching.
- [ ] `docker-compose.yml`: postgres, valkey, elasticmq (with queues + DLQs in `elasticmq.conf`), seaweedfs,
      mailpit, `init` (migrations + bucket creation), api (with reload).
- [ ] Alembic set up, first migration (`users` table) applied by `init`.
- [ ] `scripts/smoke_aws_emulators.py`: with boto3, create/put/get/presign an S3 object and send/receive/delete an SQS
      message against the emulators. **Proves the emulators work before you rely on them.**
- [ ] `ci.yml`: ruff, mypy, pytest for backend (with a testcontainers Postgres test).
- [ ] Makefile/justfile with `up`, `down`, `logs`, `test`, `lint`, `migrate`.

### Experiments
- Change one line of source code and rebuild: which layers are rebuilt? Now change `pyproject.toml`. Why the difference?
- Stop Postgres: what do `/healthz` and `/readyz` return?
- `docker compose down` vs `down -v`: what happened to your data?

### Journal prompt
Explain in your own words: image vs container; how the API container finds Postgres; why config comes from env vars.

### Checkpoint
1. Why does `postgres:5432` work inside Compose but not in your browser?
2. Why copy `pyproject.toml`/`uv.lock` and install deps **before** copying source code?
3. Why separate liveness from readiness? What would ECS do with each?
4. What happens if two developers create migrations from the same parent revision?

**Done when:** fresh clone → `make up` → `curl localhost:8000/readyz` = ok; CI green; smoke script passes.

---

## Phase 2 — Domain model, auth & admin API (≈ 2 days)

**Goal:** venues, zones, spaces, gates, devices, events (with lifecycle, pricing, zone capacities) via an authenticated,
role-protected admin API with tests.

### Concepts

**Relational modelling & constraints**
- *ELI5:* Tables are spreadsheets that point at each other; constraints are rules the database itself refuses to break, even if your code has a bug.
- *Real:* Normalise entities into tables with primary/foreign keys; encode invariants as `NOT NULL`, `UNIQUE`,
  `CHECK`, FKs. The DB is the last line of defence; app validation gives nice error messages, constraints give guarantees.
  Index columns you filter/join on; every index slows writes slightly.
- *In EventPark:* `CHECK (prebook_quota <= capacity)`, unique gate codes per venue, etc.

**Async SQLAlchemy sessions & unit of work**
- *ELI5:* A session is a shopping cart of changes; commit is paying at the till: all or nothing.
- *Real:* `AsyncSession` tracks loaded/changed objects; `async with session.begin():` defines a transaction. One
  transaction per use case, owned by the service layer. Use `selectinload` to avoid N+1 queries.

**Password hashing**
- *ELI5:* You don't store passwords; you store a fingerprint that's very slow to fake.
- *Real:* Argon2id: a slow, memory-hard, salted hash. Verification re-hashes the input. Slowness is the feature
  (brute force gets expensive).

**JWT access tokens & refresh tokens**
- *ELI5:* A JWT is a visitor badge with your name and expiry written on it and a stamp that proves the office issued it. Anyone can *read* the badge; nobody can *change* it without breaking the stamp.
- *Real:* `base64url(header).base64url(payload).signature`. Signed (HMAC/RSA), **not encrypted**, so never put secrets in it.
  Stateless: the server verifies the signature and `exp`, no DB lookup. Downside: can't revoke before expiry → keep it
  short (15 min) and pair it with a **refresh token** (long-lived, random, stored hashed server-side, revocable, rotated on use).
- *In EventPark:* access token in SPA memory; refresh token in an `HttpOnly; Secure; SameSite=Strict` cookie.

**RBAC (role-based access control)**
- *ELI5:* Your badge colour decides which doors open.
- *Real:* Users have roles; endpoints require roles; **ownership checks** (driver sees only own bookings) are extra rules.
  401 = "who are you?" (not authenticated); 403 = "I know you, but no" (not authorised).

**Machine credentials**
- *Real:* Devices aren't users: they get API keys (random, shown once, stored hashed, rotatable, revocable).

### Tasks
- [ ] Models + migrations: identity, venues, zones, spaces, gates, devices, device_commands, events, event_pricing,
      event_zones, audit_log.
- [ ] Auth: register/login/refresh(rotate)/logout/me; argon2; JWT; refresh-token families.
- [ ] 🧑‍💻 **YOU WRITE:** `require_roles(...)` FastAPI dependency + tests for 401/403/200.
- [ ] 🧑‍💻 **YOU WRITE:** the **event lifecycle state machine** in `domain/events.py`: a pure function
      `transition(current, action, now, event) -> new_status | error` + exhaustive unit tests.
- [ ] Admin CRUD endpoints (Phase-2 subset of `02-architecture.md` §8), problem+json errors, cursor pagination helper.
- [ ] Device creation/rotation returns the API key once; store prefix + hash.
- [ ] `seed` command: demo venue (3 zones incl. VIP with 50 spaces), 5 entry + 3 exit gates with devices, admin user,
      one event going live "now".
- [ ] Integration tests with testcontainers.

### Journal prompt
Design the tables yourself on paper **before** looking at `02-architecture.md` §4: what are the entities, keys, and which
rules would you make DB constraints? Then compare and explain the differences.

### Checkpoint
1. A JWT payload is readable by anyone. Why is it still secure for authentication?
2. Why are refresh tokens stored hashed? What does rotation + reuse detection protect against?
3. 401 vs 403: give an EventPark example of each.
4. Which rules did you put in `CHECK` constraints, and why not only in Python?

**Done when:** you can log in, create a venue/zones/gates/event via Swagger UI, roles are enforced, tests green.

---

## Phase 3 — The gate hot path & concurrency (≈ 3 days) ⭐ the core

**Goal:** entry/exit scan, drive-up issue with automatic zone allocation, two-phase passages, anti-passback,
idempotent retries, fee calculation. **Proven correct under concurrency.**

### Concepts

**Transactions & ACID**
- *ELI5:* A transaction is "all of these changes happen together, or none do", even if the power goes out halfway.
- *Real:* Atomicity, Consistency, Isolation, Durability. Postgres uses MVCC: readers see a snapshot; writers lock rows they change.

**Race conditions & the lost update**
- *ELI5:* Two cashiers both see "1 ticket left", both sell it. Now you've sold 2.
- *Real:* Read-then-write in application code: T1 reads 999/1000, T2 reads 999/1000, both write 1000 → one update is
  lost and the limit is broken. Happens at READ COMMITTED (the default).

**Isolation levels**
- *Real:* READ COMMITTED (each statement sees committed data), REPEATABLE READ (snapshot per transaction), SERIALIZABLE
  (as if transactions ran one by one; Postgres aborts conflicting ones with `40001`, and you must **retry**). Higher
  isolation = fewer anomalies, more aborts/retries.

**Row locks & atomic conditional updates**
- *ELI5:* Put your hand on the paper while you write on it so nobody else can grab it.
- *Real:* `SELECT … FOR UPDATE` locks rows until commit. `UPDATE … SET n = n + 1 WHERE n < cap RETURNING` does check+write
  in **one atomic statement**: the simplest correct counter. `FOR UPDATE SKIP LOCKED` = take rows nobody else holds (queues).

**Deadlocks**
- *ELI5:* Two people each holding one chopstick and waiting for the other's.
- *Real:* T1 locks A then waits for B; T2 locks B then waits for A. Postgres detects it and kills one. Prevent by always
  locking in the **same order** (our rule: event_zone → ticket → passage → payment).

**Idempotency**
- *ELI5:* Pressing the lift button five times still calls one lift.
- *Real:* An operation that has the same effect if applied once or many times. For non-idempotent operations (create entry),
  the client sends an **idempotency key**; the server stores the result under that key (unique constraint) and returns the
  stored result for repeats. Essential when networks retry.

**State machines**
- *ELI5:* A board game: from each square only certain moves are allowed.
- *Real:* Explicit states + allowed transitions, enforced in one place. Invalid transitions become errors, not silent corruption.

### Tasks
- [ ] Models/migrations: vehicles, tickets, passages, payments (table only), outbox (table only; filled from Phase 6).
- [ ] Domain: ticket + passage state machines; drive-up fee function; zone choice order.
- [ ] Device authentication dependency (`Authorization: Device …`).
- [ ] Endpoints: `entry/scan`, `entry/issue`, `passages/{id}/complete|abort`, `exit/scan` (OPEN or `PAYMENT_REQUIRED`;
      `exit/pay` comes in Phase 5), basic `heartbeat`.
- [ ] Idempotency: unique `idempotency_key` on passages; store and replay `response`; handle the race where two identical
      requests arrive simultaneously (unique violation → read and return the stored one).
- [ ] Lazy expiry of holds on scan.
- [ ] 🧑‍💻 **YOU WRITE (1):** the **naive** drive-up allocation (SELECT counters, compare in Python, UPDATE).
- [ ] 🧑‍💻 **YOU WRITE (2):** the **concurrency test**: zone with 10 free places, 200 concurrent `entry/issue` requests →
      assert exactly 10 `OPEN`, counter = capacity. Watch it **fail** against (1).
- [ ] 🧑‍💻 **YOU WRITE (3):** the fix with a conditional `UPDATE … RETURNING`; test goes green. Keep the `CHECK` constraint.
- [ ] 🧑‍💻 **YOU WRITE (4):** `calculate_drive_up_fee(...)` + unit tests: 0 min, exactly free minutes, free+1, 59/60/61 min,
      23h59, 24h, 49h, already-paid-within-grace, already-paid-after-grace.
- [ ] More concurrency tests: same ticket scanned at two entry gates simultaneously → one `OPEN`, one `ENTRY_IN_PROGRESS`;
      same `scan_id` twice concurrently → identical responses, one passage.

### Experiments (record results in your journal)
| Variant | Isolation | Expected result for 200 requests / 10 places |
|---|---|---|
| naive read-then-write | READ COMMITTED | more than 10 OPEN ❌ (or a CHECK violation error) |
| naive read-then-write | SERIALIZABLE | correct, but many `40001` errors → needs retry loop |
| conditional UPDATE | READ COMMITTED | exactly 10, no errors ✅ |
Also measure the time each variant takes.

### Journal prompt
Explain the lost-update problem using **two gates and the last free space**, with a timeline (t1, t2, …). Then
explain why one conditional `UPDATE` is safe.

### Checkpoint
1. Why do we lock the ticket row `FOR UPDATE` on scan? What could happen without it?
2. The device retries `complete` after the server already completed it. What must the server answer, and why?
3. Why store the original response for idempotent replays instead of recomputing it?
4. What's a deadlock, and how does a fixed lock order prevent it?
5. Why is the CHECK constraint still useful if the UPDATE already checks the limit?

**Done when:** all gate endpoints work in Swagger with a device key; all concurrency tests green; experiment table filled in.

---

## Phase 4 — The gate controller: your Raspberry Pi in software (≈ 1.5 days)

**Goal:** a gate-controller service that behaves like a real lane, talks to the API robustly, and can be driven by hand
(keyboard) or by the simulator.

### Concepts

**Edge vs cloud**
- *ELI5:* The gate is the "hands" on site; the cloud is the "brain" far away. The hands must behave sensibly even when the brain is slow to answer.
- *Real:* Edge devices have unreliable networks, limited resources and physical side effects. Design for timeouts and
  retries, and decide what happens on failure: **fail closed** (barrier stays shut) vs **fail open**.

**Hardware Abstraction Layer (dependency inversion)**
- *ELI5:* The program talks to "a barrier", not "GPIO pin 17". Then you can plug in a fake barrier for testing or a real one on the Pi.
- *Real:* Define interfaces (`Protocol`s) for Scanner/Printer/Barrier/LoopDetector/CardTerminal/Display; inject
  implementations. Business logic depends on abstractions, not hardware.

**Timeouts, retries, exponential backoff with jitter**
- *ELI5:* If nobody answers the door, wait a bit and knock again, waiting longer each time, and don't knock in perfect sync with 30 other people.
- *Real:* Every network call needs a timeout. Retry only **idempotent** requests (ours are, thanks to keys). Backoff
  `base·2^attempt` reduces load on a struggling server; random **jitter** prevents the **thundering herd** (all clients
  retrying at the same instant after an outage).

**Heartbeats**
- *Real:* Periodic "I'm alive" messages; absence for N intervals = offline. Piggy-back server→device commands on the
  heartbeat response (simple pull-based command channel).

### Tasks
- [ ] `gate-controller/` package: HAL protocols, simulated implementations, API client (httpx, timeouts, retries, keys).
- [ ] 🧑‍💻 **YOU WRITE:** the **lane state machine** (`machine.py`) for entry and exit lanes. You know real gates. Include
      car-leaves-before-scanning, barrier-open-but-no-car (timeout → abort), deny messages, operator manual open.
- [ ] Interactive mode: run one lane in a terminal; keys: `c` car arrives, `s` scan code, `b` press ticket button, `p` car
      passes, `r` car reverses, `q` quit; the "display" prints what a driver would see.
- [ ] Heartbeat loop + command handling (`OPEN_BARRIER`) + ack.
- [ ] Tests with a mocked API (respx): retries reuse the same `scan_id`; timeout → "call operator"; deny → barrier never opens.

### Experiments
- Stop the API container while a lane is waiting for a decision. What does the display show? Start it again and retry.
  Is there exactly one passage?
- Add 1.5 s artificial latency to the API: how does the lane feel?

### Journal prompt
Compare your state machine with how the real Pi at work behaves. What did the real one do that ours doesn't (yet)?

### Checkpoint
1. What should a gate do if the API doesn't answer within 2 s? Why?
2. Why does the device generate the `scan_id`, not the server?
3. What is the thundering herd and how does jitter help?
4. Why is a HAL worth it even if we never touch real hardware in this project?

**Done when:** you can drive a full entry → exit cycle by keyboard, including a deny and a retry after an API outage.

---

## Phase 5 — Payments: exit lanes and online booking (≈ 2 days)

**Goal:** fakepay provider, synchronous exit-lane payments with timeouts, asynchronous online booking with signed webhooks,
VIP space reservation, holds that expire.

### Concepts

**Synchronous vs asynchronous payment flows**
- *ELI5:* Card at the barrier = asking the bank and waiting a few seconds at the counter. Online booking = ordering by post and the bank sends you a letter later saying "paid".
- *Real:* Terminal charges are request/response with a timeout. Online checkout uses a **payment intent** + redirect + a
  **webhook** (server-to-server callback) as the authoritative result. Never trust the browser redirect ("success page")
  as proof of payment.

**Webhooks, HMAC signatures, replay protection**
- *ELI5:* The bank's letter has a wax seal only the bank and you know how to make, plus a date so old letters can't be resent.
- *Real:* `HMAC-SHA256(secret, timestamp + "." + raw_body)` in a header; verify with constant-time compare
  (`hmac.compare_digest`, which prevents timing attacks), reject old timestamps, dedupe by provider event id (providers
  retry and duplicate).

**Unknown outcomes & reconciliation**
- *Real:* A timeout doesn't mean failure: the charge may have happened. Mark `UNKNOWN`, retry with the **same idempotency
  key**, and reconcile by asking the provider. Never charge twice; never give away free exits silently.

**Holding inventory with expiry**
- *ELI5:* A shop puts your item aside for 15 minutes while you go to the cash machine.
- *Real:* Reserve atomically, store `expires_at`, release on expiry (lazily and via a sweeper). Never hold a DB transaction
  open during a network call. Commit the hold first, then call the provider.

### Tasks
- [ ] `fakepay/` service per `02-architecture.md` §11 (intents, checkout page, signed webhooks with retries/duplicates, terminal API).
- [ ] `exit/pay` endpoint: commit `PENDING` payment → call terminal with timeout → handle SUCCEEDED/DECLINED/UNKNOWN.
- [ ] Booking endpoints + partial unique index for VIP spaces + quota hold + 15-min expiry (lazy check now; sweeper in Phase 6).
- [ ] `/webhooks/fakepay` with verification, dedupe (`webhook_events`), booking confirmation → ticket creation.
- [ ] 🧑‍💻 **YOU WRITE:** HMAC **signing** (in fakepay) and **verification** (in the API) + tests: valid, tampered body,
      wrong secret, too old, duplicate delivery.
- [ ] 🧑‍💻 **YOU WRITE:** the payment state-machine transitions (domain) with tests.
- [ ] Gate controller: exit lane payment states; `tok_slow` path shows "please wait" and retries safely.
- [ ] Concurrency tests: 100 drivers booking the last 5 VIP spaces → exactly 5 holds; duplicate webhooks → one ticket.

### Journal prompt
Draw (ASCII is fine) the booking flow with every system involved, and mark where things can fail. For each failure, write
what the system does.

### Checkpoint
1. Why can't we confirm the booking when the browser returns to our "success" URL?
2. The terminal call timed out. Did the customer pay? What do we do?
3. Why must the booking hold be committed **before** we call fakepay?
4. Why `hmac.compare_digest` instead of `==`?

**Done when:** you can book a VIP pass in the browser via fakepay checkout and see the confirmed ticket; exit-lane payments work
including decline and timeout.

---

## Phase 6 — The async backbone: outbox, SQS, workers, S3 (≈ 2.5 days)

**Goal:** domain events flow reliably from the API to workers that compute stats, generate QR codes/emails/reports, and run
scheduled maintenance.

### Concepts

**Message queues & at-least-once delivery**
- *ELI5:* A mailbox between people who work at different speeds. Letters wait safely until someone's ready.
- *Real:* Producers and consumers are decoupled in time and scale. SQS standard queues: very high throughput,
  **at-least-once** delivery (a message can arrive twice), **no ordering guarantee**. A received message becomes invisible
  for the **visibility timeout**; if not deleted in time it reappears. After `maxReceiveCount` failures → **DLQ**.

**Idempotent consumers**
- *Real:* Because duplicates happen, processing twice must equal processing once: record `message_id` in
  `processed_messages` **in the same transaction** as the effect; set absolute values rather than increments where possible.

**The dual-write problem & transactional outbox**
- *ELI5:* Instead of "update the database AND post a letter" (one might fail), you write the letter **into the database**
  together with the update. A postman later collects all letters from the database and posts them.
- *Real:* See `02-architecture.md` §2. Guarantees "event published if and only if the transaction committed" (at least once).
  The relay uses `FOR UPDATE SKIP LOCKED`.

**Backpressure & graceful shutdown**
- *Real:* Queues absorb bursts (the API isn't slowed by slow workers); queue depth/age tell you when to scale workers.
  On `SIGTERM`, stop receiving, finish in-flight work, exit (ECS sends SIGTERM before killing).

**Object storage & presigned URLs**
- *ELI5:* S3 is a huge locker room. A presigned URL is a temporary key to one locker that expires after 5 minutes.
- *Real:* Objects addressed by bucket + key (prefixes are just key strings). Presigned URLs are signed with the creator's
  credentials and embed method, key, expiry. The browser uploads/downloads directly, so big files bypass the API.

**Scheduled jobs**
- *Real:* A scheduler emits ticks; jobs must be safe to run concurrently or twice (conditional updates, SKIP LOCKED).

### Tasks
- [ ] Outbox helper `outbox.add(session, type, data)`; add outbox writes to all Phase 3/5 use cases.
- [ ] 🧑‍💻 **YOU WRITE:** the **outbox relay** loop (batching, SKIP LOCKED, mark published, metrics: lag, batch size).
- [ ] 🧑‍💻 **YOU WRITE:** the **idempotent consumer wrapper** (receive → dedupe insert → handler → commit → delete;
      error → leave for retry; SIGTERM handling).
- [ ] Message schemas (Pydantic) + routing table (type → queue).
- [ ] worker-entry / worker-exit: upsert `hourly_stats`; (Redis read model comes in Phase 7).
- [ ] worker-notifications: QR PNG (`qrcode`) → S3 `qr/{ticket_id}.png`; email with QR to Mailpit; staff alerts.
- [ ] worker-reports: CSV (streamed) + simple PDF summary → S3; presigned download.
- [ ] Documents: presigned upload + confirm.
- [ ] worker-maintenance + `ticker`: all jobs in `02-architecture.md` §3.8.

### Experiments
- `docker compose kill worker-entry` mid-batch → messages reappear after the visibility timeout; stats still correct.
- Make one message type always throw → watch it move to the DLQ in the ElasticMQ UI (`localhost:9325`). Then fix and **redrive**.
- Stop the relay for 2 minutes during traffic → outbox grows, gates keep working → restart → catches up.
- Send the same message twice by hand → stats unchanged.

### Journal prompt
Explain the dual-write problem with an EventPark example and how the outbox solves it. Then: why must consumers be idempotent
**even with** the outbox?

### Checkpoint
1. How should the visibility timeout relate to processing time? What if it's too short? Too long?
2. What lands in a DLQ, and what do you do with it?
3. Why do workers set absolute values instead of `+1` where they can?
4. Why does the browser upload documents directly to S3?

**Done when:** a passage shows up in `hourly_stats`, a booking email with QR arrives in Mailpit, a report downloads,
holds expire automatically, and all four experiments behave as expected.

---

## Phase 7 — Redis: caching, read models, rate limiting, realtime (≈ 1.5 days)

**Goal:** fast reads, protection against abuse, live updates across multiple API instances, and graceful degradation when Redis dies.

### Concepts

**Cache-aside, TTL, invalidation**
- *ELI5:* Keep a sticky note of the answer on your desk instead of walking to the archive every time; throw the note away when the archive changes.
- *Real:* Read: cache → miss → DB → store with TTL. Write: update DB, then **delete** the key (not update: avoids races
  writing stale values). TTL bounds staleness. Watch for **cache stampede** (many misses at once → DB spike); a short lock or
  jittered TTLs help.

**Read models (CQRS-lite)**
- *Real:* A separate, query-optimised copy of data (here: occupancy snapshot per event) kept up to date asynchronously.
  Eventually consistent; rebuildable from the source of truth.

**Atomic operations & Lua**
- *Real:* Redis runs commands one at a time; a Lua script runs atomically, so "check then modify" is safe inside it.

**Rate limiting (token bucket)**
- *ELI5:* Each client gets a bucket of tokens that refills slowly; each request costs a token; empty bucket → "slow down".
- *Real:* Allows bursts up to bucket size, sustained rate = refill rate. `429 Too Many Requests` + `Retry-After`.

**Pub/sub & WebSockets**
- *ELI5:* A radio station: anyone tuned in hears every announcement. WebSockets keep a phone line open to the browser so the server can talk first.
- *Real:* Redis pub/sub is fire-and-forget fan-out (no persistence). WebSockets are full-duplex over one TCP connection.
  With N API instances, each browser is connected to one instance → instances subscribe to Redis so all of them get every
  update. Alternatives: SSE (server→client only), polling.

**Graceful degradation**
- *Real:* Non-critical dependencies fail soft: cache miss → DB, rate limiter fails open (with a metric), dashboard falls back
  to polling. Critical ones (Postgres) fail hard and visibly.

### Tasks
- [ ] 🧑‍💻 **YOU WRITE:** `get_event_config()` cache-aside with invalidation on admin updates (+ test for invalidation).
- [ ] 🧑‍💻 **YOU WRITE:** token-bucket **Lua script** + FastAPI dependency; limits for login, booking, gate endpoints.
- [ ] Occupancy read model: workers `HSET` absolute values from Postgres; maintenance rebuilds it every minute.
- [ ] `POST /admin/v1/ws-ticket` + `WS /ws/v1/events/{id}`; Redis subscriber task per API instance; heartbeat pings.
- [ ] Run **two API instances** in Compose behind a small reverse proxy (Caddy/nginx) and prove a dashboard connected
      to instance A receives updates triggered via instance B.
- [ ] Redis-down behaviour + tests.

### Experiments
- Flush Redis during traffic: what breaks? How long until the read model is back?
- Stop Redis entirely: gates must still work.
- Hammer the login endpoint: see 429s.

### Journal prompt
Which data in EventPark may be stale for a few seconds, and which must never be? Justify each.

### Checkpoint
1. Why delete the cache key on write instead of updating it?
2. Why do two API instances need Redis pub/sub for WebSockets?
3. Why does the rate limiter fail **open** but capacity checks never touch Redis?
4. What does "eventually consistent" mean for the dashboard?

**Done when:** live updates work across two API instances; Redis can be flushed or stopped without breaking gates.

---

## Phase 8 — Frontend: dashboard, operator console, booking site (≈ 3 days)

**Goal:** a usable React app for all three audiences, live-updating via WebSocket.

### Concepts

**SPA & routing**
- *ELI5:* The website is downloaded once, then it redraws parts of the page itself instead of loading new pages.
- *Real:* Client-side routing (React Router); the server must return `index.html` for unknown paths (the SPA fallback we
  configure on CloudFront later).

**Server state vs client state**
- *Real:* Server state (events, occupancy) is a **cache** of backend data: TanStack Query handles fetching, caching,
  refetching, invalidation. Client state (open dialog, form input) stays in components.

**Auth in the browser: XSS vs CSRF**
- *ELI5:* XSS = a bad script sneaks into your page and reads your stuff. CSRF = another site tricks your browser into sending a request with your cookies.
- *Real:* Access token in memory (XSS can steal it only while the page is open; not persisted). Refresh token in an
  `HttpOnly` cookie (JS can't read it) with `SameSite=Strict` (not sent on cross-site requests → CSRF-resistant).

**UI authorisation is not security**
- *Real:* Hiding a button only improves UX; the API must enforce every permission.

**Typed API clients**
- *Real:* Generate TypeScript types from the OpenAPI document so backend changes break the frontend build, not production.

### Tasks
- [ ] Vite + React + TS, Tailwind + shadcn/ui, React Router, TanStack Query, Recharts; generated API client.
- [ ] Auth pages (staff & driver), token refresh flow, role-based route guards.
- [ ] 🧑‍💻 **YOU WRITE:** `useEventStream(eventId)`: WebSocket with ws-ticket, reconnect with backoff, fallback to polling,
      patches the TanStack Query cache.
- [ ] 🧑‍💻 **YOU WRITE:** the **occupancy widget** (per-zone bars, drive-up vs pre-booked, live).
- [ ] Dashboard: cars inside (paginated), entries/exits per hour, revenue, gate activity grid (online/offline, last heartbeat,
      rejections), event history.
- [ ] Operator console: gate grid, manual open (reason required), ticket lookup, void, lost ticket, live passage feed.
- [ ] Booking site: events → zone availability (live) → VIP space picker → plate → fakepay → booking page with QR.
- [ ] Minimal admin setup screens (venues/zones/gates/devices; events/pricing/zone capacities); reports & documents.

### Checkpoint
1. Why is the access token not in localStorage?
2. What does `SameSite=Strict` protect against?
3. Why must the API re-check roles even though the UI hides buttons?
4. What happens in your UI when the WebSocket drops?

**Done when:** while the simulator (next phase) or you drive gates, the dashboard updates live, and a driver can book and see a QR.

---

## Phase 9 — Traffic simulator & local experiments (≈ 1.5 days)

**Goal:** simulate realistic and extreme traffic, prove invariants hold, find and fix bottlenecks, write it up.

### Concepts

**Load vs stress vs chaos testing**
- *Real:* Load = expected traffic (does it meet the targets?). Stress = beyond expected (where does it break, how?).
  Chaos = inject failures (does it stay correct?).

**Latency percentiles**
- *ELI5:* The average hides the unlucky drivers. p99 is "how long the slowest 1 in 100 waited".
- *Real:* p50/p95/p99 from a histogram; averages hide tails. At a gate, tail latency **is** the experience of a queue of cars.

**Little's Law**
- *Real:* `L = λ · W` (items in system = arrival rate × time in system). 2,000 cars/15 min ≈ 2.2 cars/s; if each car
  occupies a lane for 8 s (scan + barrier + drive through), then on average 2.2 × 8 ≈ **18 lanes are busy at once**.
  With 6 entry lanes, queues on the road grow during the rush, no matter how fast the server answers. The server is
  rarely the bottleneck; **the barrier is**. A real insight (and one you probably know from work).

**Finding bottlenecks**
- *Real:* Connection-pool exhaustion, N+1 queries, missing indexes (`EXPLAIN ANALYZE`), lock contention on hot rows
  (one counter row per zone!), synchronous I/O in async code.

### Tasks
- [ ] Simulator per `02-architecture.md` §12: scenarios `smoke`, `rush-2000-in-15`, `presale`, `stress-10k`, `chaos`.
- [ ] 🧑‍💻 **YOU WRITE:** `invariants.py`. You decide and code what must be true after a run (capacity, no double entries,
      counters = real counts, every completed drive-up exit has a successful payment or zero fee, no payment charged twice…).
- [ ] 🧑‍💻 **YOU WRITE:** one scenario YAML of your own based on a real event you've seen at work.
- [ ] Report generator (markdown with percentiles, throughput, decisions, invariant results).
- [ ] Find and fix **at least two** bottlenecks; document before/after in `docs/experiments/local-*.md`.
- [ ] Experiment: Redis-based admission (atomic Lua) vs Postgres conditional update: latency and complexity; write why we
      keep Postgres as the source of truth.
- [ ] Add the `smoke` scenario to CI.

### Checkpoint
1. Why p99 and not average for gate decisions?
2. Apply Little's Law to your rush scenario: is the server or the lane the bottleneck?
3. Which hot row(s) did you find, and how could you shard the contention if needed?
4. What did chaos testing reveal?

**Done when:** all scenarios pass invariants; experiment write-ups exist; CI runs the smoke scenario.

---

## Phase 10 — AWS foundations: Terraform, IAM, ECR (≈ 1 day)

**Goal:** the bootstrap stack exists (state bucket, ECR, GitHub OIDC role, alarm topic); you pushed images to ECR; you
estimated the monthly cost yourself.

### Concepts

**Infrastructure as Code & Terraform**
- *ELI5:* A recipe for your infrastructure. Anyone can cook the exact same meal from it, and you can see every change in git.
- *Real:* Declarative config (HCL): you describe the desired end state; Terraform computes a **plan** (diff between config,
  state and reality) and **applies** it via provider APIs. Building blocks: providers, resources, data sources, variables,
  outputs, locals, modules, `for_each`/`count`. `plan` is your safety net: **always read it**.

**Terraform state & locking**
- *Real:* State maps config to real resource IDs. Remote state in S3 (versioned, encrypted) with lockfile locking lets
  CI and you share it safely. Losing state = Terraform forgets what it created (import or manual cleanup).

**IAM in depth**
- *ELI5:* Who (principal) may do what (action) on which thing (resource), under which conditions.
- *Real:* Policies are JSON documents of `Effect/Action/Resource/Condition`. **Identity policies** attach to users/roles
  ("what can I do"); a role's **trust policy** says "who may become me" (assume role → temporary credentials via STS).
  Default deny; explicit deny wins. **Least privilege**: grant only what's needed, scoped to specific ARNs.

**OIDC federation**
- *Real:* GitHub issues a signed OIDC token per workflow run; AWS STS verifies it against the registered provider and trust
  policy conditions (`sub` = repo + branch) and returns short-lived credentials. No stored secrets.

**Container registries**
- *Real:* ECR stores images; `docker login` with a 12-hour token from `aws ecr get-login-password`; immutable tags (git SHA).

### Tasks
- [ ] `infra/bootstrap` with **local** state: state bucket, ECR repos + lifecycle, GitHub OIDC provider + deploy role, SNS
      alarm topic + email subscription. `terraform plan` → read it with the agent → `apply`.
- [ ] Migrate bootstrap state into the bucket it created (`terraform init -migrate-state`).
- [ ] Push `backend` and `fakepay` images to ECR manually (tag = git SHA).
- [ ] 🧑‍💻 **YOU WRITE:** the **least-privilege IAM policy** for the GitHub deploy role (see table in `03` §3). The agent
      reviews it against what `deploy.yml` will actually call.
- [ ] 🧑‍💻 **YOU WRITE:** a cost estimate in the AWS Pricing Calculator for the dev environment (8 h/day × 20 days and 24/7);
      save the link/screenshot in your journal.

### Checkpoint
1. Trust policy vs permission policy: what does each answer?
2. What exactly does `terraform plan` compare?
3. What happens if you delete the state file? How would you recover?
4. Why is OIDC safer than an access key in GitHub secrets?

**Done when:** `terraform plan` on bootstrap shows "No changes", images are in ECR, your cost estimate is written down.

---

## Phase 11 — Network & data layer on AWS (≈ 1.5 days)

**Goal:** VPC, subnets, routing, security groups, RDS, ElastiCache, SQS and S3 created by Terraform modules in `envs/dev`.

### Concepts

**IP addressing & CIDR**
- *ELI5:* `10.20.0.0/16` is a street with 65,536 house numbers; a `/24` subnet is a block of 256 houses on that street.
- *Real:* CIDR prefix = number of fixed bits. /16 → 65,536 addresses; /24 → 256 (AWS reserves 5 per subnet).
  `cidrsubnet()` in Terraform carves subnets from a VPC range.

**Public vs private subnets**
- *Real:* A subnet is "public" **only** because its route table sends `0.0.0.0/0` to an **Internet Gateway**. Private
  subnets have no such route; outbound internet would need a **NAT Gateway** (paid). Resources in private subnets are
  unreachable from the internet.

**Security groups vs NACLs**
- *ELI5:* A security group is a bouncer at each resource's door who remembers who went out (so replies can come back in).
- *Real:* SGs are stateful, allow-only, attached to network interfaces, and can reference other SGs ("allow from the app
  SG"). NACLs are stateless subnet-level allow/deny lists (we leave them default).

**Managed databases**
- *Real:* RDS handles provisioning, patching windows, automated backups, snapshots, metrics. Multi-AZ = synchronous standby
  in another AZ for failover (we skip it for cost and say why). Parameter groups = Postgres config.
  ElastiCache = managed Redis/Valkey with TLS and failover options.

### Tasks
- [ ] 🧑‍💻 **YOU WRITE:** `modules/network` (VPC, 2 public + 2 private subnets via `cidrsubnet`, IGW, route tables, optional
      NAT behind `enable_nat`, S3 gateway endpoint, security groups). The agent reviews.
- [ ] `modules/database`, `modules/cache`, `modules/queues` (`for_each` over queue names + DLQs), `modules/storage`.
- [ ] `envs/dev` wiring; S3 backend; `plan` → review → `apply` → look at everything in the console (read-only!) → `destroy`.
- [ ] Time the apply and destroy; note what was slowest.

### Experiments
- Toggle `enable_nat = true` and read the plan: what gets added, what moves, what would it cost per month?
- Try to reach the RDS endpoint from your laptop: why does it fail? (Good.)

### Checkpoint
1. What makes a subnet public? Is it a property of the subnet itself?
2. Why are RDS and ElastiCache in private subnets, and how will our ECS tasks reach them?
3. What does "stateful" mean for security groups?
4. NAT Gateway vs public IPs on tasks: cost and security trade-off in our setup?

**Done when:** `apply` creates the data layer cleanly and `destroy` removes it completely.

---

## Phase 12 — Compute on AWS: ECS, ALB, CloudFront (≈ 2.5 days)

**Goal:** the full system runs on AWS behind CloudFront; your local gate controllers and simulator drive it.

### Concepts

**ECS building blocks**
- *ELI5:* A task definition is the recipe card; a task is one dish; a service is the chef who keeps N dishes on the counter and replaces any that fall.
- *Real:* See `03` §3. Execution role vs task role. Fargate networking mode `awsvpc` gives each task its own ENI/IP.
  Rolling deployments with min/max healthy %; the **deployment circuit breaker** rolls back failing deploys automatically.

**Load balancing & health checks**
- *Real:* Listener (port/protocol) → rules (path/header conditions) → target groups (set of IPs + health check). Unhealthy
  targets are drained and replaced. WebSockets ride on the same listener (upgrade), and idle timeout matters.

**CDN & origins**
- *Real:* CloudFront behaviours route path patterns to origins (S3 for static, ALB for dynamic); cache policies decide
  what is cached (nothing for the API); **Origin Access Control** lets only CloudFront read the private bucket.

**Service discovery**
- *Real:* Service Connect gives ECS services stable DNS names inside the namespace (`fakepay`, `api`) with client-side load balancing.

**Secrets injection**
- *Real:* Task definition `secrets` reference SSM ARNs; ECS fetches them at start (execution role) and sets env vars;
  plaintext never appears in the task definition.

### Tasks
- [ ] Modules: `ecs-cluster`, `alb`, `cdn`, `scheduler`.
- [ ] 🧑‍💻 **YOU WRITE:** the generic **`ecs-service` module** (task definition, service, task role with a passed-in policy,
      optional ALB target group attachment, Service Connect, logging). The agent reviews.
- [ ] `services.tf`: api, fakepay, outbox-relay, 5 workers (Spot).
- [ ] 🧑‍💻 **YOU WRITE:** `scripts/aws-up.sh` (apply → migrate task → seed task → print URLs/keys) and `scripts/aws-down.sh`.
- [ ] Upload the frontend build to S3; open the CloudFront URL; log in.
- [ ] Point local gate controllers + simulator `smoke` at the CloudFront URL. Watch the dashboard.
- [ ] ECS Exec into an api task, run `psql`, look at passages.
- [ ] Deliberately deploy a broken image (e.g. `/readyz` always 500) → watch the circuit breaker roll back.

### Checkpoint
1. Trace one gate scan from the device to RDS and back, naming every AWS hop.
2. Execution role vs task role: which one reads SSM at startup? Which one sends to SQS?
3. Why `target_type = ip`?
4. What happened when you deployed the broken image, step by step?

**Done when:** `aws-up.sh` → working system on a CloudFront URL → smoke simulation passes on AWS → `aws-down.sh`.

---

## Phase 13 — CI/CD with GitHub Actions (≈ 1 day)

**Goal:** every push is tested; every merge to `main` builds, migrates and deploys automatically when the env is up.

### Concepts

**Pipelines, artifacts, immutable tags**
- *Real:* CI produces an artifact (image) tagged with the commit SHA; the same artifact is promoted/deployed. `latest` is
  ambiguous: you can't tell what's running or roll back precisely.

**Deployment strategies**
- *Real:* Rolling (ECS default: replace tasks gradually), blue/green (two full sets, switch traffic, e.g. via CodeDeploy),
  canary (small % first). We use rolling + circuit breaker.

**Zero-downtime migrations (expand/contract)**
- *ELI5:* Don't remove the old road until all cars use the new one.
- *Real:* During a rolling deploy, old and new code run **at the same time** against the **same** DB. So migrations must be
  backward compatible: add columns (nullable) → deploy code using them → backfill → later remove old columns.
  Dropping/renaming a column used by old code = errors mid-deploy.

### Tasks
- [ ] Complete `ci.yml` per `03` §5 (incl. compose smoke test, terraform fmt/validate, gitleaks).
- [ ] `deploy.yml`: OIDC → build/push → (env up?) → register task defs → migrations → update services → wait → frontend
      sync + invalidation → write image tag to SSM.
- [ ] 🧑‍💻 **YOU WRITE:** the migration step (run-task, wait, check exit code, fail the job on failure).
- [ ] Branch protection on `main` (CI required); Dependabot for pip/npm/actions/terraform.
- [ ] Do one real feature via PR end-to-end (e.g. "show average stay duration on dashboard").

### Checkpoint
1. Why tag images with the SHA?
2. Why run migrations before updating services, and what makes a migration unsafe during a rolling deploy?
3. What prevents a fork's PR from deploying to your AWS account?

**Done when:** a merged PR appears on the CloudFront URL without you touching AWS.

---

## Phase 14 — Observability & scaling on AWS (≈ 2 days)

**Goal:** you can see what the system is doing, get alerted when it misbehaves, and watch it scale under load. With written results.

### Concepts

**Three pillars: logs, metrics, traces**
- *ELI5:* Logs = a diary of everything that happened. Metrics = the car's dashboard gauges. Traces = following one parcel through every sorting centre.
- *Real:* Logs (detailed, expensive to search at scale), metrics (cheap aggregates, for alarms/graphs), traces (per-request
  causality across services; our `request_id` is a lightweight version).

**Golden signals, SLIs/SLOs, actionable alarms**
- *Real:* Latency, traffic, errors, saturation. An **SLI** is a measurement (gate decisions < 300 ms); an **SLO** is a target
  (99% over 1 h). Alarm on symptoms users feel, and only on things someone should act on.

**Autoscaling**
- *Real:* Target tracking (keep a metric near a target: CPU 60%, requests per target), step scaling, cooldowns. Scale
  workers on **queue backlog per task**, not CPU (an idle-waiting worker has low CPU even with a huge backlog).

**Saturation of shared resources**
- *Real:* Scaling the API multiplies DB connections. Pool size × tasks must stay below `max_connections`. Horizontal
  scaling moves the bottleneck; it doesn't remove it.

### Tasks
- [ ] EMF metrics in code (`GateDecisionLatency`, `GateDecisions`, `OutboxLag`, `DevicesOffline`, …).
- [ ] `modules/observability`: alarms + dashboard per `03` §3; log retention.
- [ ] 🧑‍💻 **YOU WRITE:** Logs Insights queries: "follow one request_id across services", "slowest 20 scans", "rejections by
      reason per gate"; and an **SLO** definition for gate decisions.
- [ ] Autoscaling: API (requests + CPU), entry/exit workers (backlog per task).
- [ ] Load tests on AWS: (a) API fixed at 1 task; (b) autoscaling 2–6; compressed rush scenario. Record p50/p95/p99,
      scale-out timeline, DB connections, queue age. Hit the DB connection limit on purpose and explain it.
- [ ] Trigger each alarm once on purpose (poison message, stopped gate controller, failing deploy).
- [ ] Write `docs/experiments/aws-*.md` with graphs (screenshots) and conclusions.

### Checkpoint
1. Why alarm on p95 latency instead of average?
2. What is "saturation" in EventPark? Name three signals.
3. Why scale workers on backlog instead of CPU?
4. You doubled API tasks and latency didn't improve. Why might that be?

**Done when:** dashboard + alarms work, scaling experiment written up with numbers.

---

## Phase 15 — Cognito, security review, portfolio (≈ 1.5 days)

**Goal:** managed identity with Cognito, a security pass, and a portfolio-ready repository.

### Concepts

**Managed identity (Amazon Cognito)**
- *ELI5:* Instead of building your own passport office, you use the government's; you just check the stamps.
- *Real:* User pools store users, handle sign-up/sign-in/MFA/password reset, and issue OIDC tokens (ID token = who you are;
  access token = what you may call). Groups → our roles. The API verifies tokens with Cognito's **JWKS** public keys (RS256),
  checking `iss`, `aud`/`client_id`, `exp`, `token_use`.

**Migration strategy**
- *Real:* Feature flag `AUTH_PROVIDER=local|cognito`; both verifiers behind one interface; migrate users (or require reset).

### Tasks
- [ ] Terraform: Cognito user pool, app client, groups (ADMIN, EVENT_MANAGER, GATE_OPERATOR, PARKING_STAFF, DRIVER),
      managed login domain.
- [ ] 🧑‍💻 **YOU WRITE:** the JWKS-based token verification dependency (cache keys, check claims) + tests.
- [ ] Frontend login via Cognito (authorization code + PKCE).
- [ ] Security review with the checklist in `03` §8; fix findings.
- [ ] README for the portfolio: architecture diagram, screenshots, the experiment results, "what I learned", how to run it.
      Record a 3-minute demo video (simulator rush with live dashboard).
- [ ] Final `terraform destroy`; keep bootstrap or destroy it too.

### Checkpoint
1. ID token vs access token?
2. Why can the API verify Cognito tokens without calling Cognito on every request?
3. What were the top three findings of your security review?

**Done when:** you can explain every box in the architecture diagram, and how and why it's built that way.

---

## After the project: stretch goals

- **Offline gate mode**: Ed25519-signed QR codes verifiable on the Pi, local anti-passback cache, later sync and conflict resolution.
- **Run gate-controller on a real Raspberry Pi** with a USB scanner, ESC/POS printer and GPIO relay.
- **Licence-plate recognition** with YOLO at entry lanes (camera → plate → vehicle record).
- **AWS IoT Core** (MQTT + X.509 certificates) for device connectivity.
- **SNS → SQS fan-out**, **RDS Proxy**, **CloudFront VPC origins** (private ALB), **blue/green** with CodeDeploy, **OpenTelemetry/X-Ray** tracing.

## The CV line you'll have earned

> Built and deployed **EventPark**, a cloud-native event-parking platform: FastAPI + PostgreSQL gate API with
> race-free capacity control and idempotent edge-device protocol, transactional outbox → SQS workers, Redis caching /
> rate limiting / realtime WebSockets, React dashboard, and a traffic simulator (10k vehicles). Infrastructure on
> AWS (ECS Fargate, ALB, CloudFront, RDS, ElastiCache, SQS, S3, CloudWatch) fully in Terraform, deployed via GitHub
> Actions with OIDC; load-tested with autoscaling and alarms.
