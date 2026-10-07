# CLAUDE.md — EventPark

> Instructions for AI coding agents (Claude Code, Codex/Astra, etc.) working in this repository.
> `AGENTS.md` points here. Read this file fully before doing anything.

## 1. What this project is

**EventPark** is a cloud platform for running parking at large events (stadium, concert, fair).
Thousands of vehicles enter and leave through multiple gates. Each gate behaves like the real
Raspberry-Pi-based gates Damjan has installed at work: QR scanner + ticket printer + barrier +
loop detector (+ card terminal at exit lanes), asking the cloud API on every scan.

- Drivers either **pre-book** a pass online (QR code) or take a **drive-up** printed ticket at
  the entry and **pay at the exit lane terminal**.
- Capacity is counted per zone; VIP/reserved spots are numbered spaces.
- Staff use a dashboard (live occupancy, gate activity, revenue, history) and an operator console.
- A traffic simulator drives thousands of virtual cars to test concurrency and scaling.
- Runs locally in Docker first, then on AWS (ECS/Fargate, RDS, ElastiCache, SQS, S3, CloudFront,
  CloudWatch) built **only** with Terraform and deployed with GitHub Actions.

Full spec lives in `docs/`. Start with `docs/README.md`.

## 2. This is a LEARNING project — the teaching contract

Damjan is a computing-engineering student with little prior AWS/cloud experience. The goal is
that **he understands and can explain every part**, not that the code exists. The architecture is
intentionally production-style: **do not simplify the architecture to make implementation easier.**
If something seems over-engineered, the reason is in `docs/decisions.md` — read it before
proposing changes.

How to work with him:

1. **Phase discipline.** Work follows `docs/04-learning-path.md`. Check `docs/progress.md` to
   see which phase is active. Do not jump ahead to later phases or build features from future
   phases "while you're there".
2. **Explain before you build.** At the start of each phase (and each new concept) give:
   - an **ELI5** (2–4 sentences, everyday analogy), then
   - the **real explanation** (precise, with the correct terms), then
   - **why this project needs it**.
   Keep it concise and practical. Offer a simplified version alongside technical ones.
3. **He writes first, you correct.** Damjan learns by writing his own understanding and getting
   corrections. Before implementing a concept, ask him to write his explanation/plan in
   `docs/journal/phase-XX.md`; then review it: say what is right, what is wrong, and why.
4. **"YOU WRITE" tasks are his.** Tasks marked `YOU WRITE` in the learning path are for Damjan to
   implement. Do not write that code. You may give hints, review, point to docs, write tests for it,
   or show a *different* example of the same idea.
5. **Ask targeted questions before large outputs.** Before generating many files or a big change,
   state the plan in a few bullets and ask the 1–3 questions that matter.
6. **Small steps, runnable at each step.** Prefer many small commits that each leave the system
   working. After each step, tell him how to run/verify it himself.
7. **Checkpoints.** End each phase by asking the phase's checkpoint questions from the learning
   path and discussing his answers. Then update `docs/progress.md`.
8. **Record decisions.** Any non-trivial design choice → new entry in `docs/decisions.md`
   (context, options, decision, consequences). Never silently change an existing decision.

## 3. Architecture invariants (do not violate)

- **PostgreSQL is the source of truth.** Redis holds caches, read models, rate-limit buckets and
  pub/sub only. Anything in Redis must be rebuildable from Postgres.
- **The gate hot path is synchronous and fast.** A scan gets a decision (OPEN / DENY /
  PAYMENT_REQUIRED) in one DB transaction. Side effects (stats, notifications, QR images,
  reports, realtime pushes) are **asynchronous** via the outbox → SQS → workers.
- **Never publish to SQS (or call any network service) inside a DB transaction.** Use the
  transactional outbox table; the outbox relay publishes after commit.
- **Every gate request carries an idempotency key** (`scan_id` / `request_id`) and must be safe
  to retry. Every SQS consumer must be idempotent (messages arrive at least once).
- **Capacity can never be exceeded**, even under concurrent requests. Enforced in the database
  (conditional `UPDATE ... WHERE ... RETURNING`, constraints, row locks) — never by
  read-then-write in Python.
- **Money is integer minor units** (`amount_minor`, RSD para) + `currency`. Never floats.
- **Time is stored in UTC** (`timestamptz`); converted to the venue timezone only for display.
- **App roles ≠ AWS IAM.** App roles (ADMIN, EVENT_MANAGER, GATE_OPERATOR, PARKING_STAFF,
  DRIVER) are checked in the API. IAM is only for AWS resources (which ECS task may read which
  queue, etc.).
- **All AWS resources come from Terraform** (`infra/`). No click-ops, except the one-time
  account/billing setup in Phase 0. If something was changed in the console for learning, it must
  be reverted or imported.
- **No secrets in git.** Local: `.env` (gitignored) with `.env.example` committed. AWS: SSM
  Parameter Store SecureString, injected into ECS tasks.
- **Same code, different config.** Local vs AWS differ only by environment variables
  (endpoints, credentials). No `if ENV == "aws"` branches in business logic.

## 4. Cost safety (AWS) — always follow

- **Never run `terraform apply` or `terraform destroy` without Damjan's explicit go-ahead** in
  the current conversation. Always show `terraform plan` output first and summarise what will be
  created/destroyed and roughly what it costs per hour.
- The `dev` environment is **ephemeral**: up while working, `terraform destroy` at the end of the
  session. Remind him at the end of every session that touched AWS.
- The `bootstrap` stack (state bucket, ECR, GitHub OIDC role, budgets) stays up; it costs ~cents.
- Do not add paid services not in the spec (NAT Gateway is behind a variable, default off; no
  WAF, no Multi-AZ RDS, no Container Insights) without asking and stating the cost.
- **The account stays on the AWS Free plan (ADR-022). Zero spend is a hard requirement.** Never suggest upgrading the
  plan, creating an AWS Organization, enabling IAM Identity Center with Organizations, Control Tower, or anything else
  that auto-upgrades the account. If a needed service isn't available on the Free plan, propose a design change.
- Region: `eu-central-1` (Frankfurt).

## 5. Repository map

```
.
├── CLAUDE.md / AGENTS.md      # agent instructions (this file)
├── README.md                  # portfolio-facing overview
├── docker-compose.yml         # full local stack
├── .env.example
├── backend/                   # Python package `eventpark` (API + workers + outbox relay)
│   ├── src/eventpark/
│   │   ├── api/               # FastAPI routers: admin, gate, public, ws
│   │   ├── domain/            # pure business logic (pricing, rules, state machines) — no I/O
│   │   ├── services/          # use cases; orchestrate domain + repositories
│   │   ├── db/                # SQLAlchemy models, session, repositories
│   │   ├── messaging/         # outbox, SQS client, message schemas
│   │   ├── workers/           # entry, exit, notifications, reports, maintenance consumers
│   │   ├── cache/             # Redis: cache-aside, occupancy read model, rate limit, pub/sub
│   │   ├── storage/           # S3 wrapper (put, presigned URLs)
│   │   └── core/              # config, security (JWT, hashing), logging, errors
│   ├── migrations/            # Alembic
│   └── tests/                 # unit / integration / concurrency
├── gate-controller/           # Python edge-device service (the "Raspberry Pi")
├── fakepay/                   # simulated payment provider (FastAPI)
├── simulator/                 # traffic simulator + invariant checker
├── frontend/                  # React + TS + Vite: dashboard, operator console, booking site
├── infra/
│   ├── bootstrap/             # one-time: state bucket, ECR, OIDC, budgets
│   ├── modules/               # network, database, cache, queues, storage, ecs-service, alb, cdn, observability
│   └── envs/dev/              # the ephemeral environment
├── .github/workflows/         # ci.yml, deploy.yml
└── docs/                      # spec, learning path, decisions, progress, journal, study notes
```

(Directories appear as their phase is reached. Keep this map updated.)

## 6. Tech stack (pinned choices)

- **Backend:** Python 3.13, FastAPI, Pydantic v2 + pydantic-settings, SQLAlchemy 2.x (async) +
  asyncpg, Alembic, redis-py (asyncio), boto3, structlog. Package manager **uv**.
- **Quality:** ruff (lint + format), mypy (strict on `domain/`), pytest, pytest-asyncio,
  testcontainers (real Postgres/Redis in tests), moto (S3/SQS in tests).
- **Frontend:** React + TypeScript + Vite, React Router, TanStack Query, Tailwind + shadcn/ui,
  Recharts, openapi-typescript/openapi-fetch (typed client generated from the API's OpenAPI).
- **Local infra (docker compose):** postgres:17, valkey (Redis-compatible), ElasticMQ (SQS),
  SeaweedFS (S3-compatible), Mailpit (fake SMTP inbox).
- **AWS:** ECS on Fargate (+ Fargate Spot for workers), ECR, ALB, RDS PostgreSQL 17,
  ElastiCache (Valkey), SQS (+DLQs), S3, CloudFront, EventBridge Scheduler, SSM Parameter Store,
  CloudWatch, IAM, VPC. Terraform ≥ 1.11 with AWS provider ~> 6.x, S3 backend with native locking.
- **CI/CD:** GitHub Actions with OIDC to AWS (no long-lived access keys).

## 7. Commands

Fill these in as they are created (Phase 1+). Keep them accurate.

```bash
# local stack
docker compose up -d            # start infra + services
docker compose logs -f api
# backend
cd backend && uv sync
uv run pytest                   # all tests
uv run ruff check . && uv run ruff format --check . && uv run mypy src
uv run alembic upgrade head
# frontend
cd frontend && npm ci && npm run dev
# simulator
cd simulator && uv run python -m simulator run scenarios/rush.yaml
# infra
cd infra/envs/dev && terraform init && terraform plan
```

## 8. Coding conventions

- Type hints everywhere; Pydantic models at API boundaries; domain logic in `domain/` is pure
  (no DB, no network) so it is trivially unit-testable.
- Routers are thin: validate → call a service → map result. Business rules live in services/domain.
- One DB transaction per use case; the service owns the transaction boundary.
- Errors: domain exceptions → mapped to HTTP problem responses in one place.
- Logs are structured JSON with `request_id`, `event_id`, `gate_id`, `ticket_id` where relevant.
  Never log secrets, full QR codes, or card data.
- Tests: every bug fix gets a test; every invariant in §3 has a test (concurrency tests included).
- Migrations: one Alembic revision per schema change; never edit an applied migration.
- Commits: conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `infra:`), small and focused.

## 9. Where to look

- `docs/README.md` — how the docs fit together
- `docs/01-product-and-domain.md` — what we're building, rules, state machines
- `docs/02-architecture.md` — components, flows, data model, API
- `docs/03-aws-and-infrastructure.md` — AWS design, Terraform, CI/CD, cost
- `docs/04-learning-path.md` — **the phases we follow**
- `docs/05-glossary.md` — ELI5 glossary
- `docs/decisions.md` — why things are the way they are
- `docs/progress.md` — where we are right now
