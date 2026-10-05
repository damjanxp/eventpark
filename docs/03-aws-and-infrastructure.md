# 03 — AWS & Infrastructure

How EventPark runs on AWS, how it's created (Terraform), deployed (GitHub Actions), monitored
(CloudWatch), and **what it costs**.

> **ELI5:** AWS is a giant warehouse of computers and services you rent by the hour. Instead of clicking
> around to rent things (and forgetting what you clicked), you write a **shopping list** (Terraform). Run
> `terraform apply` and AWS builds everything on the list. Run `terraform destroy` and it all goes away,
> and so does the bill. GitHub Actions is a robot that, every time you push code, tests it, packs it
> into containers and ships it to the warehouse.

---

## 1. Target architecture

```
                                   Internet
             ┌────────────────────────┼─────────────────────────────┐
         Browsers                Gate devices (on site / your PC)    fakepay checkout (browser)
             └────────────────────────┼─────────────────────────────┘
                                      ▼  HTTPS (*.cloudfront.net certificate, free)
                              ┌───────────────┐
                              │  CloudFront   │  /*        → S3 frontend bucket (Origin Access Control)
                              │               │  /api/* /gate/* /webhooks/* /ws/* /fakepay/* → ALB
                              └───────┬───────┘  (adds secret header X-Origin-Verify)
 ┌──────────────── VPC 10.20.0.0/16 (eu-central-1, 2 AZs) ───────────────────────────────────────┐
 │                                    ▼                                                          │
 │  Public subnets   ┌──────────────────────────────┐  SG: only CloudFront prefix list :80        │
 │  (AZ a, AZ b)     │  Application Load Balancer   │  rule: header must match, else 403          │
 │                   └──────┬────────────────┬──────┘                                             │
 │                          ▼                ▼                                                    │
 │   ECS Fargate tasks: [api ×2..6] [fakepay]  [outbox-relay] [worker-entry] [worker-exit]        │
 │   (public IP, no inbound    │         ▲      [worker-notifications] [worker-reports]           │
 │    except from ALB SG)      │ Service Connect                     [worker-maintenance]         │
 │                             ▼                                                                 │
 │  Private subnets   ┌───────────────┐   ┌─────────────────────┐                                │
 │  (AZ a, AZ b)      │ RDS PostgreSQL│   │ ElastiCache (Valkey) │   no internet access at all    │
 │                    └───────────────┘   └─────────────────────┘                                │
 └───────────────────────────────────────────────────────────────────────────────────────────────┘
   Regional services (outside the VPC, reached over HTTPS with IAM):
   SQS (5 queues + 5 DLQs) · S3 (frontend, files) · ECR · SSM Parameter Store · CloudWatch Logs/Metrics/Alarms
   · EventBridge Scheduler (tick → maintenance queue) · SNS (alarm emails)
```

**Why tasks in public subnets with public IPs, not private subnets + NAT Gateway?** A NAT Gateway costs
~$0.05/hour **plus** per-GB processing, even when idle. For a learning env that is created and destroyed daily, giving
tasks public IPs (with a security group that allows **no inbound traffic** except from the ALB) is cheaper
and still safe. The `enable_nat` Terraform variable switches to the textbook layout (tasks in private subnets,
outbound through NAT) so you can learn it and compare. Databases are **always** private. See `decisions.md` ADR-012.

---

## 2. Account setup & safety (Phase 0, the only click-ops)

1. Create the AWS account. New accounts (since July 2025) get **$100 in credits, plus up to $100 more** for
   completing onboarding activities, on a **free plan** that lasts **6 months or until credits run out**.
   **We stay on the Free plan for the whole project (ADR-022):** on the Free plan AWS *cannot* charge you; when
   credits run out or 6 months pass, the account closes instead. **Never** click "Upgrade plan", never create an
   AWS Organization (that upgrades automatically and voids remaining credits). If a service we need turns out not to
   be available on the Free plan, we change the design, not the plan.
2. Secure the **root user**: strong password + **MFA**. Then never use root again.
3. Create an **IAM user** for yourself with `AdministratorAccess` and **MFA**, and **no access keys**. (Not IAM
   Identity Center: giving it access to AWS accounts requires AWS Organizations, which would end the Free plan;
   see ADR-022.) Configure the CLI with `aws login` (CLI ≥ 2.32: browser sign-in → short-lived credentials) so you never
   store long-lived access keys on disk.
4. **Budgets** (Billing → Budgets): a monthly cost budget of **$25** with email alerts at 20/50/80/100%, set to
   show costs **before credits** so you see the real burn. Plus a **daily** budget of **$5**.
5. Enable **Cost Anomaly Detection** (free) and turn on **Cost Explorer**.
6. Pick region **eu-central-1 (Frankfurt)**: closest to Serbia, has every service we need.

---

## 3. AWS services, one by one

For each: ELI5 → what we use → key settings → cost (approx., Frankfurt, verify with the AWS Pricing
Calculator; that's an exercise in Phase 10).

### VPC (Virtual Private Cloud)
- **ELI5:** your own fenced-off private network inside AWS, with gates (route tables) deciding which roads lead to the internet.
- **We use:** `10.20.0.0/16`, two Availability Zones; public subnets `10.20.0.0/24`, `10.20.1.0/24`
  (ALB, ECS tasks); private subnets `10.20.10.0/24`, `10.20.11.0/24` (RDS, ElastiCache); Internet Gateway;
  public route table `0.0.0.0/0 → IGW`; private route table has no internet route (unless `enable_nat`).
  S3 **gateway endpoint** (free) attached to both route tables.
- **Cost:** VPC itself free. **Public IPv4 addresses cost $0.005/hour each** (ALB uses 2, each task 1).

### Security Groups (stateful firewalls per resource)
| SG | Inbound | Outbound |
|---|---|---|
| `alb` | TCP 80 from CloudFront origin-facing **managed prefix list** | to `app` SG |
| `app` (api, fakepay) | 8000 from `alb` SG; 8000 from `app` SG (Service Connect) | all |
| `worker` | **none** | all |
| `db` | 5432 from `app` + `worker` SGs | none |
| `cache` | 6379 from `app` + `worker` SGs | none |

### ECR (Elastic Container Registry)
- **ELI5:** a private shelf for your Docker images.
- **We use:** repos `eventpark/backend`, `eventpark/fakepay` (in the **bootstrap** stack, so they survive
  destroy); images tagged with the **git SHA**; lifecycle policy keeps the last 15 images; scan on push.
- **Cost:** ~$0.10/GB-month. A few images ≈ cents.

### ECS on Fargate
- **ELI5:** you hand AWS a container and say "keep 2 of these running". Fargate means you never see or manage
  the servers.
- **Concepts:** *cluster* (a namespace) → *task definition* (blueprint: image, CPU/memory, env, secrets, logs,
  roles) → *task* (a running copy) → *service* (keeps N tasks running, replaces failed ones, registers them
  with the ALB, does rolling deploys).
- **Two IAM roles per task** (you *will* mix these up at first):
  - **Execution role**: used by *ECS itself* to pull the image from ECR, fetch secrets from SSM, write logs.
  - **Task role**: used by *your code* (boto3) to call SQS, S3, etc.
- **We use:** one cluster; services below; **deployment circuit breaker with rollback**; `enable_execute_command`
  (ECS Exec, to open a shell in a running task for debugging); **Service Connect** namespace so `api` can call
  `http://fakepay:8000` and vice versa; **Fargate Spot** for workers (up to ~70% cheaper, can be interrupted with
  a 2-minute warning, which is fine for idempotent queue consumers).

| Service | CPU / Mem | Count | Capacity provider |
|---|---|---|---|
| api | 0.5 vCPU / 1 GB | 2 (autoscale 2–6) | Fargate |
| fakepay | 0.25 / 0.5 | 1 | Fargate |
| outbox-relay | 0.25 / 0.5 | 1 | Fargate |
| worker-entry, worker-exit | 0.25 / 0.5 | 1 (autoscale 1–5) | Fargate Spot |
| worker-notifications, worker-reports, worker-maintenance | 0.25 / 0.5 | 1 | Fargate Spot |

- **Cost:** ~$0.047/vCPU-hour + ~$0.005/GB-hour (x86). Whole fleet ≈ **$0.11/hour**.

### ALB (Application Load Balancer)
- **ELI5:** a receptionist that spreads visitors over your API copies and stops sending people to a copy that looks sick.
- **We use:** HTTP :80 listener (CloudFront terminates HTTPS); rules: header `X-Origin-Verify` must match a
  secret (else fixed 403); path `/fakepay/*` → fakepay target group; default → api target group. Target type
  `ip` (required for Fargate). Health check `GET /readyz`. Idle timeout 120 s (WebSockets ping every 25 s).
  Deregistration delay 30 s.
- **Cost:** ~$0.027/hour + LCU usage (small) ≈ **$0.03/hour**.

### CloudFront
- **ELI5:** copies of your website stored close to users worldwide, plus a single front door with free HTTPS.
- **We use:** one distribution, default domain `dxxxx.cloudfront.net` (free TLS certificate; no custom domain needed).
  Behaviours: default → S3 frontend bucket via **Origin Access Control** (bucket stays private) with SPA fallback
  (403/404 → `/index.html`); `/api/*`, `/gate/*`, `/webhooks/*`, `/ws/*`, `/fakepay/*` → ALB origin, **caching
  disabled**, all headers/cookies/query strings forwarded, WebSockets supported. Adds the `X-Origin-Verify` header.
  Gate devices also talk to the API through CloudFront, so they get HTTPS for free.
- **Cost:** always-free tier covers this project. Creation/deletion takes **5–15 min**, the slowest part of `apply`.

### S3
- **ELI5:** an infinitely large, very reliable hard drive you talk to over HTTP. Files are "objects" in "buckets".
- **We use:** `eventpark-dev-frontend-<account>` (SPA build) and `eventpark-dev-files-<account>` with prefixes
  `qr/`, `reports/`, `documents/`. Block Public Access ON; SSE-S3 encryption; CORS on files bucket allowing `PUT/GET`
  from the CloudFront domain (presigned uploads); lifecycle: `reports/` expire after 7 days, abort incomplete
  multipart uploads after 1 day. `force_destroy = true` **in dev only** (otherwise destroy fails on non-empty buckets).
- **Cost:** pennies.

### SQS
- **ELI5:** a mailbox. Someone drops a letter; a worker picks it up; if the worker drops it on the floor, it
  reappears in the mailbox after a while; letters that keep failing go to a "problem letters" box (DLQ).
- **We use:** standard queues `entry-events`, `exit-events`, `notifications`, `reports`, `maintenance`, each with a
  DLQ (redrive `maxReceiveCount = 5`), SSE enabled, long polling (`ReceiveMessageWaitTimeSeconds = 20`),
  DLQ retention 14 days.
- **Cost:** first 1M requests/month free. Long polling keeps empty receives low.

### RDS for PostgreSQL
- **ELI5:** PostgreSQL that AWS installs, patches and backs up for you.
- **We use:** Postgres 17, `db.t4g.micro`, 20 GB gp3, **single-AZ**, private subnets, `publicly_accessible = false`,
  `storage_encrypted = true`, backup retention 1 day, `skip_final_snapshot = true`, `deletion_protection = false`
  (dev only!), parameter group with `log_min_duration_statement = 200` (log slow queries). Password generated by
  Terraform (`random_password`) → SSM SecureString.
- **To look inside:** ECS Exec into an api task and run `psql` (no bastion host, no public DB).
- **Cost:** ≈ $0.02/hour + storage. Creation takes ~5–10 min. Data is **disposable** in dev: a seed task recreates it.

### ElastiCache (Valkey)
- **ELI5:** a super-fast in-memory notepad shared by all API copies.
- **We use:** Valkey (Redis-compatible, cheaper than Redis OSS on ElastiCache), `cache.t4g.micro`, 1 node, private
  subnets, **in-transit encryption** (so the URL is `rediss://`).
- **Cost:** ≈ $0.015–0.02/hour. Creation ~5–10 min.

### SSM Parameter Store
- **ELI5:** a locked drawer for passwords and settings; only roles you allow can open it.
- **We use:** `SecureString` params `/eventpark/dev/db_password`, `jwt_secret`, `fakepay_webhook_secret`,
  `origin_verify_secret`; ECS injects them as env vars via the task definition `secrets` block (execution role
  needs `ssm:GetParameters` + `kms:Decrypt`). Standard tier is free. (Secrets Manager adds rotation for
  $0.40/secret/month; see ADR-014.)

### EventBridge Scheduler
- **We use:** a schedule `rate(1 minute)` that sends `{"type":"tick"}` to the `maintenance` queue.
- **Cost:** free tier covers it.

### CloudWatch
- **Logs:** one log group per service, **retention 3 days** (cost control).
- **Metrics:** AWS ones (ALB, ECS, SQS, RDS, ElastiCache) + ours via **EMF** log lines.
  ⚠️ Custom metrics cost ~$0.30 per metric per month (prorated hourly), and **every unique dimension combination is a
  separate metric**. Never use high-cardinality values (ticket_id, user_id) as dimensions.
- **Alarms** → SNS topic → your email:

| Alarm | Condition |
|---|---|
| API 5xx | ALB `HTTPCode_Target_5XX_Count` / requests > 1% for 5 min |
| API latency | ALB `TargetResponseTime` p95 > 0.5 s for 5 min |
| Gate decision latency | custom `GateDecisionLatency` p95 > 300 ms |
| Queue stuck | `ApproximateAgeOfOldestMessage` > 60 s on entry/exit queues |
| Poison messages | any DLQ `ApproximateNumberOfMessagesVisible` > 0 |
| Outbox lag | custom `OutboxLag` > 30 s |
| Devices offline | custom `DevicesOffline` > 0 for 2 min |
| RDS | CPU > 80%, `FreeStorageSpace` < 2 GB |
| ECS | running task count < desired for 5 min |

- **Dashboard** (`eventpark-dev`): ALB requests/latency/5xx; ECS CPU/memory per service; queue depth/age per queue;
  RDS CPU/connections; gate decision latency p50/p95/p99; decisions by type; occupancy.

### IAM — least privilege per component
| Principal | Allowed |
|---|---|
| ECS **execution role** (shared) | pull from ECR, write logs, read `/eventpark/dev/*` SSM params, decrypt with the SSM KMS key |
| **api** task role | `sqs:SendMessage`? **No**, the API never talks to SQS (outbox!). `s3:PutObject/GetObject` on `documents/*`, `qr/*`, `reports/*` (presigned URLs are only valid if the signer has the permission; `HeadObject` is covered by `s3:GetObject`); ECS Exec permissions (`ssmmessages:*`) |
| **outbox-relay** task role | `sqs:SendMessage` on the 5 queues; nothing else |
| **worker-*** task roles | `sqs:ReceiveMessage/DeleteMessage/ChangeMessageVisibility` on **its own** queue; S3 put on its prefix (notifications → `qr/`, reports → `reports/`); `cloudwatch:PutMetricData` not needed (EMF) |
| **EventBridge Scheduler** role | `sqs:SendMessage` on `maintenance` only |
| **GitHub Actions** (OIDC) role | push to the 2 ECR repos; `ecs:RegisterTaskDefinition`, `ecs:UpdateService`, `ecs:RunTask`, `ecs:Describe*` on the cluster; `iam:PassRole` for the task/execution roles; `s3:ListBucket/PutObject/DeleteObject` on the frontend bucket (for `aws s3 sync`); `cloudfront:CreateInvalidation`; `ssm:PutParameter` on `/eventpark/dev/image_tag` |
| **You** | IAM user with AdministratorAccess + MFA, CLI via `aws login` (short-lived credentials, no access keys) |

> Notice what IAM is here: permissions for **software and people acting on AWS resources**. It has nothing to do
> with "can a GATE_OPERATOR open a barrier"; that's our app's RBAC.

---

## 4. Terraform

### Layout
```
infra/
├── bootstrap/                 # applied ONCE, stays up (cheap)
│   ├── main.tf                #  - S3 state bucket (versioned, encrypted, private)
│   │                          #  - ECR repos + lifecycle policies
│   │                          #  - GitHub OIDC provider + deploy role
│   │                          #  - SNS alarm topic + email subscription
│   │                          #  - (optional) budgets as code
│   └── (local state at first → then migrated into the bucket it created: a classic chicken-and-egg lesson)
├── modules/
│   ├── network/               # VPC, subnets, IGW, routes, optional NAT, S3 gateway endpoint, SGs
│   ├── database/              # RDS, subnet group, parameter group, password → SSM
│   ├── cache/                 # ElastiCache Valkey
│   ├── queues/                # for_each over queue names → queue + DLQ + redrive
│   ├── storage/               # buckets, policies, CORS, lifecycle
│   ├── alb/                   # ALB, listener, rules, target groups
│   ├── ecs-cluster/           # cluster, capacity providers, Service Connect namespace, execution role, log groups
│   ├── ecs-service/           # GENERIC: task def + service + task role + optional ALB attachment + autoscaling
│   ├── cdn/                   # CloudFront, OAC, behaviours
│   ├── scheduler/             # EventBridge Scheduler → maintenance queue
│   └── observability/         # alarms, dashboard, metric filters
└── envs/
    └── dev/
        ├── versions.tf        # terraform >= 1.11, aws ~> 6.0, random
        ├── backend.tf         # s3 backend: bucket, key = "dev/terraform.tfstate", use_lockfile = true
        ├── main.tf            # wires modules together
        ├── services.tf        # one module "ecs-service" block per service
        ├── variables.tf       # enable_nat, api_desired_count, image_tag, alarm_email, ...
        ├── outputs.tf         # cloudfront_url, alb_dns, queue urls, cluster name, ...
        └── terraform.tfvars   # gitignored if it contains anything personal
```

Conventions: `provider "aws" { default_tags { tags = { Project = "eventpark", Env = "dev", ManagedBy = "terraform" } } }`;
names `eventpark-dev-<thing>`; every module has `variables.tf`, `outputs.tf`, `README.md` (what it creates, why).

### State
- **ELI5:** Terraform's memory of what it built. Lose it and Terraform no longer knows those resources are "his".
- Stored in S3 with **versioning** (undo) and **native S3 locking** (`use_lockfile = true`, Terraform ≥ 1.10/1.11;
  no DynamoDB table needed anymore), so two `apply`s can't run at once.
- State can contain secrets (the DB password) → the bucket is private and encrypted; never commit `*.tfstate`.

### The daily ephemeral workflow
```bash
./scripts/aws-up.sh     # terraform apply (≈15–20 min, CloudFront/RDS/ElastiCache are slowest)
                        # → run one-off ECS task: alembic upgrade head
                        # → run one-off ECS task: seed (venue, zones, gates, devices, demo event)
                        # → print CloudFront URL + device API keys
... work, deploy, load-test ...
./scripts/aws-down.sh   # terraform destroy (≈10–15 min) → $0/hour again (bootstrap stays)
```
What survives `destroy`: bootstrap (state bucket, ECR images, OIDC role, SNS topic). Everything else is recreated
identically: **that's the proof your infrastructure is really code.**

### Image tags vs Terraform (a classic conflict)
Terraform creates the task definitions using `image_tag` read from SSM parameter `/eventpark/dev/image_tag`.
CI deploys by registering a **new task definition revision** with the new image and updating the service, then
writes the tag to that SSM parameter. ECS services in Terraform use `lifecycle { ignore_changes = [task_definition] }`
so Terraform doesn't "fight" CI and roll back the deployment. Next day's `apply` starts from the last deployed tag. (ADR-016)

---

## 5. CI/CD (GitHub Actions)

### Authentication: OIDC, not access keys
- **ELI5:** instead of giving GitHub a permanent key to your AWS house, AWS trusts GitHub's ID card for a few
  minutes, and only when the card says "this is Damjan's repo, branch main".
- Bootstrap creates an IAM OIDC provider for `token.actions.githubusercontent.com` and a role whose trust policy
  requires `aud = sts.amazonaws.com` and `sub = repo:<you>/eventpark:ref:refs/heads/main` (or `environment:dev`).
- Workflows use `aws-actions/configure-aws-credentials` with `role-to-assume`. **No AWS secrets in GitHub.**

### Workflows
**`ci.yml`**: on every push & PR:
1. backend: `uv sync` → ruff → mypy → pytest (testcontainers works on GitHub's Ubuntu runners)
2. gate-controller, fakepay, simulator: lint + tests
3. frontend: `npm ci` → typecheck → lint → vitest → build
4. `terraform fmt -check` + `terraform validate` (+ `tflint`)
5. docker build (no push) to prove images build
6. e2e smoke: `docker compose up` → simulator `smoke.yaml` (200 cars) → invariants must pass

**`deploy.yml`**: on push to `main` (after CI) and manual `workflow_dispatch`:
1. Assume the OIDC role.
2. Build & push `backend` and `fakepay` images tagged `${{ github.sha }}` (+ layer cache).
3. If the ECS cluster doesn't exist (env is down) → stop here with a notice (images are ready for next `aws-up`).
4. Register new task definition revisions (render with new image).
5. **Run migrations** as a one-off ECS task (`alembic upgrade head`) and wait; fail the deploy if it fails.
6. Update all services → wait for stability (`aws ecs wait services-stable`); circuit breaker rolls back on failure.
7. Build frontend → `aws s3 sync dist/ s3://…frontend --delete` → CloudFront invalidation `/index.html`.
8. Write `image_tag` to SSM; post a summary (URLs, SHA) to the job summary.

Make the repo **public** for the portfolio (unlimited Actions minutes on public repos) and double-check that nothing
secret is committed (a `gitleaks` step in CI helps).

---

## 6. Scaling

- **API:** target tracking on `ALBRequestCountPerTarget` (e.g. 1,000 req/min per task) **and** CPU 60%;
  min 2 / max 6; scale-out cooldown 60 s, scale-in 300 s.
- **Workers (entry/exit):** scale on **backlog per task** (`ApproximateNumberOfMessagesVisible / running tasks`)
  using target tracking with metric math; min 1 / max 5.
- **Database:** vertical only (bigger instance class). Watch `DatabaseConnections`: every API task has a pool
  (e.g. 10) → 6 tasks × 10 + workers… must stay below `max_connections` of `db.t4g.micro` (~80-ish). A real lesson
  in Phase 14. (RDS Proxy is the managed fix, costs extra; it's discussed but not deployed.)
- **The honest takeaway** you'll discover: *2,000 cars in 15 minutes is only ~2–5 requests/second*. That's trivial
  for one API task. To see scaling you'll **compress time** in the simulator (e.g. 60×) to reach hundreds of
  requests/second. Knowing the difference between "realistic load" and "stress test" is itself a valuable insight.

---

## 7. Cost estimate (dev, Frankfurt, approximate; verify yourself in Phase 10)

| Item | ≈ $/hour while up |
|---|---|
| Fargate (all services, workers on Spot) | 0.11 |
| ALB | 0.03 |
| RDS db.t4g.micro + storage | 0.02 |
| ElastiCache cache.t4g.micro (Valkey) | 0.02 |
| Public IPv4 (~11 addresses) | 0.055 |
| CloudFront, S3, SQS, Scheduler, SSM, logs | ~0.01 |
| **Total** | **≈ $0.23–0.30/hour → ≈ $2–2.5 per 8-hour day** |
| NAT Gateway (only if `enable_nat = true`) | +0.05 + data |
| **Forgotten for a month (24/7)** | **≈ $170–220 ❗ (= all your credits)** |

Bootstrap stack when everything else is destroyed: ≈ $0.01–0.10/day (S3 state, ECR storage).

Rules: destroy at end of every session; budgets + anomaly detection on; check Cost Explorer each Monday.

---

## 8. Security checklist (Phase 15 review)

- [ ] Root has MFA and no access keys; daily work through an IAM user with MFA and no access keys (`aws login`)
- [ ] No long-lived AWS keys anywhere (CI uses OIDC; tasks use roles)
- [ ] RDS & ElastiCache in private subnets, SGs allow only app/worker SGs
- [ ] ALB reachable only from CloudFront (prefix list + secret header)
- [ ] S3 buckets private (Block Public Access), CloudFront via OAC, files via short-lived presigned URLs
- [ ] Secrets only in SSM SecureString; not in task definition plain env, not in git, not in logs
- [ ] Each task role can touch only its own queue/prefix
- [ ] Encryption at rest (RDS, S3, SQS) and in transit (CloudFront HTTPS, ElastiCache TLS)
- [ ] Rate limiting on login, booking and gate endpoints
- [ ] Dependency scanning (Dependabot) + ECR image scan + gitleaks

---

## 9. Local ↔ AWS mapping

| Concern | Local | AWS |
|---|---|---|
| Containers | docker compose | ECS Fargate services |
| Images | local build | ECR |
| Entry point / HTTPS | localhost ports, Vite proxy | CloudFront → ALB |
| Database | postgres:17 container | RDS PostgreSQL 17 |
| Cache | valkey container | ElastiCache Valkey |
| Queues | ElasticMQ | SQS |
| Files | SeaweedFS | S3 |
| Email | Mailpit | (SES sandbox, stretch) / logged |
| Scheduler | `ticker` container | EventBridge Scheduler |
| Secrets | `.env` | SSM Parameter Store |
| Service discovery | compose DNS names | ECS Service Connect |
| Logs/metrics | `docker compose logs` | CloudWatch |
| Credentials | dummy keys | IAM task roles |
