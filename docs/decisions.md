# Architecture Decision Records

Each decision: **context → decision → alternatives considered → consequences**. New decisions get the next
number. Superseded decisions stay, marked *Superseded by ADR-xxx*. When you wonder "why not just…?", look here first.

---

### ADR-001 — Monorepo, one backend image with many entrypoints
- **Context:** API, outbox relay and five workers share models, config and domain logic.
- **Decision:** one repo; `backend/` is one Python package and one Docker image; ECS services differ only by `command`.
- **Alternatives:** a repo/image per service (real microservices).
- **Consequences:** shared code without packaging pain; one image to build and scan. Services still scale and fail
  independently because they run as separate ECS services.

### ADR-002 — Python 3.13 + FastAPI (async)
- **Context:** you know Python; the simulator/gate controller are Python; async suits I/O-heavy APIs and WebSockets.
- **Decision:** FastAPI + SQLAlchemy 2 async + asyncpg; uv for packaging.
- **Alternatives:** Spring Boot, NestJS, ASP.NET.
- **Consequences:** one language across backend, devices, simulator. Must avoid blocking calls in async code (boto3 is sync →
  run in a threadpool or keep it in workers/relay).

### ADR-003 — PostgreSQL is the source of truth; Redis is never authoritative
- **Context:** capacity, tickets and payments must never be wrong.
- **Decision:** all correctness-critical state and checks in Postgres (constraints, row locks, conditional updates). Redis only
  for cache, read models, rate limits, pub/sub, all rebuildable.
- **Alternatives:** Redis counters for admission (faster).
- **Consequences:** simpler failure reasoning (Redis loss = slower, not wrong). Phase 9 measures what Redis admission would gain.

### ADR-004 — Synchronous gate decisions, asynchronous side effects
- **Context:** a driver waits at the barrier; stats/emails/reports can wait seconds.
- **Decision:** gate endpoints decide in one DB transaction and return; everything else goes through the outbox → SQS → workers.
- **Alternatives:** queue the entry itself and let the gate poll for a decision.
- **Consequences:** low, predictable gate latency; the dashboard is eventually consistent (~1–2 s).

### ADR-005 — Transactional outbox instead of publishing to SQS from the API
- **Context:** the dual-write problem (DB commit and SQS send can't be atomic).
- **Decision:** write events to `outbox` in the business transaction; a relay publishes with `FOR UPDATE SKIP LOCKED`.
- **Alternatives:** publish after commit (lost events on crash); Postgres logical replication/CDC (Debezium), too heavy here.
- **Consequences:** at-least-once delivery → all consumers idempotent; an extra process to run and monitor (`OutboxLag`).
  The API task role needs **no** SQS permissions.

### ADR-006 — Online validation on every scan with opaque ticket codes
- **Context:** your real system's Pis ask the server on every scan; simplest correct anti-passback.
- **Decision:** QR/barcode contains a random opaque code; the server is the only authority.
- **Alternatives:** signed codes validated offline on the device.
- **Consequences:** no network = no entry (fail closed; operator manual open exists). Offline mode is a documented stretch goal.

### ADR-007 — Two-phase passages with expiring holds
- **Context:** "barrier opened" ≠ "car passed"; cars reverse away, loops misfire.
- **Decision:** `AUTHORIZED` (hold capacity/ticket state) → `COMPLETED` on loop signal; holds expire after 60 s.
- **Alternatives:** single-step entry (count on authorize).
- **Consequences:** counts reflect reality; needs expiry logic (lazy + scheduled) and more device↔API calls.

### ADR-008 — Capacity counters with atomic conditional updates
- **Context:** the hottest, most race-prone operation.
- **Decision:** counters on `event_zones` + `UPDATE … WHERE counter < limit RETURNING` + `CHECK` constraints.
- **Alternatives:** `COUNT(*)` per request (slow, still racy); SERIALIZABLE with retries; Redis.
- **Consequences:** O(1), correct at READ COMMITTED. One hot row per zone limits write throughput (fine for our scale;
  Phase 9 discusses sharding counters if needed). Counters can drift on bugs → reconciliation + invariant checks.

### ADR-009 — Zone counting, numbered spaces only for VIP/reserved
- **Context:** real event car parks count zones; VIP products sell specific spots.
- **Decision:** drive-up and normal pre-booked = zone counters; VIP/reserved = numbered spaces with a partial unique index.
- **Consequences:** realistic; teaches both counter-based and constraint-based concurrency control.

### ADR-010 — Standard SQS queues per consumer, routed by the relay
- **Context:** the attachment's queue split (entry, exit, notifications, reports) + maintenance ticks.
- **Decision:** 5 standard queues + DLQs; the relay routes by message type.
- **Alternatives:** FIFO queues (ordering + dedupe, lower throughput, more complexity); SNS topic fan-out to queues.
- **Consequences:** consumers must not rely on order (they read current state from Postgres). SNS fan-out is a stretch goal.

### ADR-011 — Simulated payment provider (fakepay)
- **Context:** need realistic payment behaviour (async webhooks, declines, timeouts, duplicates) without external accounts.
- **Decision:** own small FastAPI service with configurable failure modes; Stripe-like API shape.
- **Consequences:** we can test nasty cases on demand; swapping to Stripe later would touch one adapter.

### ADR-012 — ECS tasks in public subnets with public IPs; NAT Gateway optional
- **Context:** tasks need outbound access (ECR, SQS, S3, CloudWatch); NAT Gateway ≈ $0.05/h + data even when idle.
- **Decision:** tasks get public IPs in public subnets; SGs allow inbound only from the ALB (workers: none). Data stores are
  private. `enable_nat` variable switches to the private-subnet + NAT layout for learning.
- **Alternatives:** NAT (textbook), VPC interface endpoints (≈ $0.01/h each per AZ; 5+ needed, which costs more than NAT).
- **Consequences:** cheaper and still safe for dev; in production you'd usually choose private subnets. With ~11 public IPv4s
  at $0.005/h each, the cost difference is smaller than people expect, which is a good thing to discover yourself.

### ADR-013 — Ephemeral dev environment
- **Context:** ~$200 credits; a 24/7 environment would cost ~$170–220/month.
- **Decision:** `aws-up.sh` / `aws-down.sh` every session; bootstrap stack persists; dev data is disposable (seed script).
- **Consequences:** ~15–20 min startup; forces truly reproducible infrastructure (a feature).

### ADR-014 — SSM Parameter Store for secrets (not Secrets Manager)
- **Decision:** `SecureString` parameters (free standard tier), injected via ECS `secrets`.
- **Alternatives:** Secrets Manager ($0.40/secret/month, automatic rotation, RDS-managed passwords).
- **Consequences:** no automatic rotation; Terraform state contains the generated DB password → state bucket must be private.

### ADR-015 — Local emulators: ElasticMQ (SQS) + SeaweedFS (S3); moto in tests
- **Context (2025–26):** MinIO stopped publishing community Docker images and was archived; LocalStack Community now
  requires an account/auth token.
- **Decision:** ElasticMQ for SQS, SeaweedFS for S3 in Docker Compose; **moto** (in-process) for automated tests;
  fallback for S3: `adobe/s3mock`. Phase 1 smoke script verifies them.
- **Consequences:** no account needed; two small containers; code uses standard boto3 endpoint env vars, so switching is config only.

### ADR-016 — Image tag handoff between Terraform and CI
- **Context:** Terraform defines task definitions; CI deploys new images. Both "own" the image → they fight.
- **Decision:** Terraform reads the current tag from SSM `/eventpark/dev/image_tag`; CI registers new task-definition
  revisions, updates services and writes the tag back; services ignore `task_definition` changes in Terraform.
- **Consequences:** clean daily recreate; deploys don't need Terraform permissions in CI.

### ADR-017 — Own JWT auth first, Cognito later
- **Decision:** implement auth yourself (Phase 2) to understand it; migrate to Cognito behind a flag (Phase 15).
- **Consequences:** more code early; real understanding of tokens, hashing, rotation; Cognito becomes a meaningful comparison.

### ADR-018 — CloudFront as the single front door; ALB only reachable from CloudFront
- **Decision:** browsers and gate devices use the CloudFront URL (free HTTPS); ALB SG allows only the CloudFront
  origin-facing prefix list; listener requires a secret `X-Origin-Verify` header.
- **Alternatives:** public ALB with ACM certificate (needs a domain); CloudFront **VPC origins** with an internal ALB (stretch).
- **Consequences:** HTTPS without a domain; CloudFront→ALB hop is HTTP (acceptable for dev; documented).

### ADR-019 — ECS on Fargate (not EKS, EC2 or Lambda)
- **Decision:** Fargate: containers without managing servers or Kubernetes.
- **Alternatives:** EKS (control plane ~$0.10/h + complexity), EC2 (patching), Lambda (WebSockets/long workers awkward,
  cold starts on the gate path).
- **Consequences:** container skills transfer anywhere; per-task cost higher than EC2 at scale (irrelevant here).

### ADR-020 — Single-AZ RDS, no RDS Proxy, no Multi-AZ (dev cost)
- **Decision:** `db.t4g.micro` single-AZ; connection pools sized deliberately.
- **Consequences:** an AZ failure takes the DB down (acceptable for dev; production would use Multi-AZ). Connection
  limits become a visible scaling lesson in Phase 14.

### ADR-021 — WebSockets with Redis pub/sub fan-out
- **Decision:** API instances subscribe to `realtime:event:{id}`; workers publish; one-time ws-tickets for browser auth.
- **Alternatives:** polling only; SSE; API Gateway WebSocket APIs.
- **Consequences:** works across N instances; messages are fire-and-forget (clients refetch on reconnect).

### ADR-022 — Stay on the AWS Free plan; IAM user + `aws login` instead of IAM Identity Center
- **Context:** Damjan's hard requirement: learn AWS without paying anything. Since July 2025 new accounts choose a
  **Free plan** or **Paid plan**. On the Free plan AWS does not charge: when the credits ($100 + up to $100 for
  onboarding activities) are used up or 6 months pass, the account is closed (data kept 90 days, then deleted).
  Creating an **AWS Organization** (and Control Tower, etc.) **automatically upgrades** the account to Paid, and the
  remaining Free-plan credits expire immediately. IAM Identity Center can only grant access to AWS accounts from an
  *organization* instance (account instances have no permission sets), so it needs Organizations.
- **Decision:** the account stays on the Free plan for the whole project. Daily access is an **IAM user** with
  `AdministratorAccess` + MFA and **no access keys**; the CLI uses `aws login` (AWS CLI ≥ 2.32), which issues
  short-lived credentials after a browser sign-in. This replaces the "Identity Center + `aws configure sso`" setup in
  `03` §2.
- **Alternatives:** Paid plan with budgets (budgets only alert, they don't stop charges); Identity Center via an
  Organization (ends the Free plan); IAM user with long-lived access keys (works, but a leaked key is a real risk);
  LocalStack only (free Hobby plan, but no real AWS, so the AWS phases lose most of their learning value).
- **Consequences:** the worst case is "the account closes", never "a bill". Budgets and anomaly detection remain
  useful as **early warnings** that credits are burning. All AWS phases (10–15) must finish within 6 months of sign-up.
  If a service turns out not to be available on the Free plan, we adapt the design (new ADR) instead of upgrading.
  Expected spend: about $2–2.5 per 8-hour AWS day × about 10 days ≈ $25–40 of credits.
