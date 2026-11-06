# Progress

**Active phase:** Phase 8 — Frontend
**AWS dev environment:** ⛔ down   *(update whenever you run aws-up / aws-down)*

| # | Phase | Est. days | Status | Journal | Notes |
|---|---|---|---|---|---|
| 0 | Foundations & safe AWS account | 1 | ✅ | [phase-00](journal/phase-00.md) | budget Credit filter still TODO |
| 1 | Local walking skeleton + CI | 1.5 | ✅ | [phase-01](journal/phase-01.md) | CI green; checkpoint answers deferred |
| 2 | Domain model, auth & admin API | 2 | ✅ | [phase-02](journal/phase-02.md) | journal + checkpoint deferred |
| 3 | Gate hot path & concurrency ⭐ | 3 | ✅ | | experiment: [phase-03-allocation](experiments/phase-03-allocation.md); journal + checkpoint deferred |
| 4 | Gate controller (the Pi) | 1.5 | ✅ | | experiments: [phase-04](experiments/phase-04-gate-controller.md); journal + checkpoint deferred |
| 5 | Payments & booking | 2 | ✅ | | journal + checkpoint deferred |
| 6 | Outbox, SQS, workers, S3 | 2.5 | ✅ | | experiments: [phase-06](experiments/phase-06-async.md); journal + checkpoint deferred |
| 7 | Redis: cache, read model, rate limit, realtime | 1.5 | ✅ | | experiments: [phase-07](experiments/phase-07-redis.md); ADR-026/027; journal + checkpoint deferred |
| 8 | Frontend | 3 | ⬜ | | |
| 9 | Simulator & local experiments | 1.5 | ⬜ | | |
| 10 | AWS foundations: Terraform, IAM, ECR | 1 | ⬜ | | |
| 11 | Network & data layer on AWS | 1.5 | ⬜ | | |
| 12 | ECS, ALB, CloudFront | 2.5 | ⬜ | | |
| 13 | CI/CD | 1 | ⬜ | | |
| 14 | Observability & scaling | 2 | ⬜ | | |
| 15 | Cognito, security, portfolio | 1.5 | ⬜ | | |

Status: ⬜ not started · 🟨 in progress · ✅ done · ✂️ cut (see cut list in `04-learning-path.md`)

## Session log
<!-- One line per working session: date — phase — what got done — AWS up/down — next step -->
- 2026-10-05 — Phase 0 — AWS account (Free plan), root MFA, IAM user `damjan.petrov` in group `Admins` + MFA, no access keys, billing access for IAM, `aws login` works (eu-central-1), budgets `eventpark-monthly` $25 + `eventpark-daily` $5 created — AWS: nothing running — **TODO 2026-10-06:** edit both budgets → Scope → Charge type → Excludes → Credit (Cost Explorer data wasn't available yet); then GitHub repo, journal, checkpoint
- 2026-10-07 — Phase 0 — study notes (`docs/study/phase-00.md`), journal + agent review, gate spec updated from real-world review (arming loop, ticket-taken sensor, printer paper status, manual exit "released unpaid", tailgating accepted), checkpoint discussed → **Phase 0 done** — AWS: nothing running — next: fix budget Credit filter; start Phase 1
- 2026-10-07 — Phase 1 — journal answers reviewed; backend skeleton (config, JSON logging, /healthz), request-ID
  middleware, docker compose (postgres on host port 5433, valkey, elasticmq, seaweedfs, mailpit), /readyz +
  testcontainers tests; readiness experiment (stop Postgres) done — AWS: nothing running — next: Dockerfile + api/init
  services, then Alembic + users table, smoke script, CI, Makefile
- 2026-10-28 — Phase 1 — Dockerfile (multi-stage, non-root, code in own layer), api + init services, Alembic +
  `users` table (ADR-023: UUIDv7 in Python, enums as CHECK), emulator smoke script (10/10), local bucket creation,
  Makefile, ci.yml (both jobs pass on a fresh clone locally); all 3 experiments recorded in the journal — AWS:
  nothing running — next: push to GitHub (CI must go green), Phase 1 checkpoint, then Phase 2
- 2026-10-28 — Phase 2 (prep) — `core/security.py` (argon2id, HS256 JWT, refresh-token hashing) + tests;
  `journal/phase-02.md` created (table design exercise) — tables/endpoints wait for Damjan's design
- 2026-11-04 — Phase 1 → CI green on GitHub. Phase 2 — all tables + migration, auth (login/refresh rotation +
  reuse detection/logout/register/me), require_roles, event lifecycle state machine, admin CRUD (users, venues, zones,
  spaces, gates, devices, events, pricing, zones), audit log, seed (`make seed`), ADR-024 (device keys SHA-256) —
  journals/checkpoints deferred by Damjan — AWS: nothing running — next: Phase 3
- 2026-11-04 — Phase 3 — tickets/passages/vehicles/outbox tables, gate API (entry scan, drive-up issue with atomic
  allocation, complete/abort, exit scan with fee, heartbeat/commands), idempotency incl. concurrent duplicates, lazy
  hold expiry, fee function, concurrency tests, allocation experiment (naive: 191 tickets for 10 places), ADR-025 —
  AWS: nothing running — next: Phase 4
- 2026-11-04 — Phase 4 — gate-controller package (HAL protocols + simulated hardware, client with timeout/retries/
  backoff+jitter, lane state machine entry/exit incl. payment states, heartbeat + commands), `make lane GATE=N1`,
  API-outage experiment (1 passage), init now mounts migrations (stale-image bug), CI job — next: Phase 5
- 2026-11-04 — Phase 5 — fakepay service (intents, checkout, signed webhooks w/ retries + duplicates, terminal with
  test tokens, idempotency), bookings/payments/webhook_events tables, exit/pay (commit PENDING → call → record;
  timeout = UNKNOWN), booking holds with lazy expiry, VIP partial unique index, webhook verify + dedupe, payment state
  machine, live e2e booking + tok_slow exit verified; httpx moved to runtime deps (caught by the container) — next: Phase 6
- 2026-11-04 — Phase 6 — outbox relay (SKIP LOCKED, batches), idempotent consumer (dedupe in the same tx, side
  effects outside, visibility extension, SIGTERM), workers entry/exit (hourly_stats in venue tz), notifications (QR → S3,
  email → Mailpit), reports (CSV streamed, PDF), maintenance (expire holds/bookings, event clock, devices offline,
  payment reconciliation, cleanup), ticker, documents via presigned PUT, archive → summary; moto server tests;
  outage experiments done — next: Phase 7
- 2026-11-04 — Phase 7 (mostly) — token-bucket Lua rate limits (login per IP/email, booking per user, gate per
  device; fail open; trusted-proxy X-Forwarded-For), cache-aside event config with DEL-after-commit, occupancy read
  model (absolute values, rebuilt by maintenance), ws-ticket + WS endpoint, Redis pub/sub subscriber per instance,
  workers publish after commit, zone-threshold alerts; 250 tests green — AWS: nothing running.
  **NEXT SESSION:** (1) check CI (runs were stuck "queued" on GitHub's side); (2) Phase 7 leftovers: compose profile
  with 2 API instances behind Caddy + live cross-instance demo, `docs/experiments/phase-07-redis.md`;
  (3) Phase 8 frontend; (4) Phase 9 simulator; (5) Phases 10-15 Terraform/CI/CD written but NOT applied
  (needs Damjan: `aws login` + explicit go-ahead). Journals/checkpoints for phases 1-7 deferred by Damjan.
- 2026-11-06 — CI red since Phase 6 (Compose v5 `up --wait` rejects containers without a healthcheck) → heartbeat-file
  liveness for workers/relay/ticker (ADR-026), CI green. Phase 7 finished: `scale` profile (api-2 + Caddy :8080),
  `make demo-scale` (update crosses instances in 162 ms), experiments: Redis stop made gates take 2–4 s → short
  timeouts + circuit breaker (ADR-027, now ~30 ms) — Damjan: agent also implements YOU WRITE tasks for now, journals
  deferred — AWS: nothing running

## Spending log
<!-- Monday check of Cost Explorer: date — month-to-date cost (before credits) — credits left -->
