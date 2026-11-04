# Progress

**Active phase:** Phase 3 — Gate hot path & concurrency
**AWS dev environment:** ⛔ down   *(update whenever you run aws-up / aws-down)*

| # | Phase | Est. days | Status | Journal | Notes |
|---|---|---|---|---|---|
| 0 | Foundations & safe AWS account | 1 | ✅ | [phase-00](journal/phase-00.md) | budget Credit filter still TODO |
| 1 | Local walking skeleton + CI | 1.5 | ✅ | [phase-01](journal/phase-01.md) | CI green; checkpoint answers deferred |
| 2 | Domain model, auth & admin API | 2 | ✅ | [phase-02](journal/phase-02.md) | journal + checkpoint deferred |
| 3 | Gate hot path & concurrency ⭐ | 3 | ⬜ | | |
| 4 | Gate controller (the Pi) | 1.5 | ⬜ | | |
| 5 | Payments & booking | 2 | ⬜ | | |
| 6 | Outbox, SQS, workers, S3 | 2.5 | ⬜ | | |
| 7 | Redis: cache, read model, rate limit, realtime | 1.5 | ⬜ | | |
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

## Spending log
<!-- Monday check of Cost Explorer: date — month-to-date cost (before credits) — credits left -->
