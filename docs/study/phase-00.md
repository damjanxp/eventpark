# Phase 0 — Study notes

Everything you need to know before writing `journal/phase-00.md` and answering the Phase 0 checkpoint.
Each topic: **ELI5** (the intuition) → **Real explanation** (what you'd say in an interview) →
**In EventPark** (why it matters here) → **Common mistakes** → **Test yourself**.

How to use this file: read a section, close it, then explain the topic out loud or on paper. If you get stuck,
reread only that part. The journal must be written **from memory, in your own words**. Copying from here
teaches you nothing, and the review only helps if it shows what *you* actually think.

Contents:
1. Cloud computing and service models
2. Regions, Availability Zones and global services
3. Shared responsibility
4. Identity: root, IAM users, groups, roles, policies, MFA, credentials
5. Billing: how AWS charges, the Free plan, credits, budgets, what actually stops charges
6. The EventPark architecture, from gate scan to dashboard update
7. Preparing the "real gate system" section (questions only, no answers, on purpose)

---

## 1. Cloud computing and service models

**ELI5:** Instead of buying computers and putting them in a cupboard, you rent them from a huge company by the
hour. You can also rent ready-made machines, like "a database" or "a mailbox for messages", and the company
keeps them running, patched and backed up for you.

**Real explanation:**
Cloud computing is **on-demand, self-service access to computing resources over an API, billed by usage**.
Five properties define it (the NIST definition):
- **On-demand self-service**: you create a server or database yourself in seconds, no sales call.
- **Broad network access**: everything is reachable and controllable over the network (console, CLI, API).
- **Resource pooling**: many customers share the same physical hardware, isolated from each other.
- **Rapid elasticity**: scale up and down quickly, even automatically.
- **Measured service**: you pay for what you use (per second, hour, GB, request).

Service models, from "you manage a lot" to "you manage almost nothing":

| Model | You manage | Provider manages | AWS examples |
|---|---|---|---|
| **On-premises** (not cloud) | everything, including the building and hardware | nothing | your firm's server room |
| **IaaS** (Infrastructure as a Service) | OS, runtime, app, data | hardware, network, virtualisation | EC2 (virtual machines), VPC, EBS disks |
| **PaaS / managed services** | your app/data and configuration | OS, patching, backups, the software itself | RDS (Postgres), ElastiCache, SQS |
| **Serverless** | only your code/container + config | everything else, including servers and scaling | Fargate, Lambda, S3 |
| **SaaS** | just use it | everything | Gmail, GitHub |

Key trade-offs:
- **CapEx → OpEx**: no big upfront hardware purchase (capital expense); instead a monthly bill (operating expense).
- **Elasticity**: you can have 100 servers for one hour and 2 the rest of the day, and pay accordingly.
- **Managed services save work but cost more per unit** and limit control (you can't SSH into RDS).
- **The bill is now your responsibility**: forgotten resources keep charging. In the old world a forgotten server
  only cost electricity.

**In EventPark:** almost everything is a managed or serverless service: RDS instead of installing Postgres on a VM,
SQS instead of running RabbitMQ, Fargate instead of managing EC2 machines. So you learn **architecture**, not
server administration. Because billing is per hour, the dev environment exists only while you work
(`terraform apply` in the morning, `terraform destroy` in the evening, ADR-013).

**Common mistakes:**
- "Serverless means there are no servers." There are; you just don't see or manage them.
- "Cloud is always cheaper." It's cheaper to *start* and to handle spiky load. For steady 24/7 load, owned
  hardware can be cheaper. Cloud wins on speed and flexibility.

**Test yourself:**
1. Where does RDS sit in the table, and what does AWS do for you that it wouldn't on EC2?
2. Why does per-hour billing change how we run the dev environment?

---

## 2. Regions, Availability Zones and global services

**ELI5:** A **region** is a city where AWS has data centres (Frankfurt). An **Availability Zone (AZ)** is one
building (or a group of buildings) in that city, with its own power, cooling and internet connection, far enough
from the others that a fire or flood hits only one. If you keep a copy of your app in two buildings, one building
burning down doesn't take you offline.

**Real explanation:**
- A **region** is a separate geographic area, e.g. `eu-central-1` (Frankfurt), `eu-north-1` (Stockholm),
  `us-east-1` (N. Virginia). Regions are **isolated from each other** by design: a failure in one doesn't spread.
  Data you put in a region **stays there** unless you move it (important for law, e.g. GDPR).
- Each region has **at least 3 AZs**. An AZ is one or more data centres with **independent power, cooling and
  networking**, physically separated (kilometres apart), connected to the other AZs by **low-latency, high-bandwidth**
  private fibre (single-digit milliseconds).
- AZ names like `eu-central-1a` are **shuffled per account**: your `1a` may be someone else's `1b`. The stable
  names are **AZ IDs** like `euc1-az1`. (A nice interview detail.)
- **Edge locations** are a third, smaller kind of site (hundreds worldwide) used by CloudFront to cache content
  close to users.

Services exist at different "scopes":

| Scope | Meaning | Examples |
|---|---|---|
| **Global** | one per account, the same everywhere | IAM, Billing/Budgets, CloudFront, Route 53 |
| **Regional** | lives in one region, AWS spreads it across AZs for you | SQS, S3 buckets, ECR, VPC, ALB, Lambda |
| **Zonal** | lives in **one** AZ; if that AZ fails, it fails | a subnet, an EC2 instance, an EBS disk, a single-AZ RDS instance |

You saw this yourself: the IAM console showed **"Global"** in the region selector, and its URL said
`us-east-1.console.aws.amazon.com`. Global services are *run* from us-east-1, but they apply to every region.

**High availability (HA)** = the system keeps working when a part fails. The basic AWS recipe is
**"spread across ≥2 AZs"**: run copies in two AZs behind a load balancer; if one AZ dies, the other keeps serving.

**Why the ALB needs two AZs:** an Application Load Balancer is the front door for all traffic. AWS **refuses to
create one** unless you give it subnets in at least two AZs, because a front door that lives in a single building is
a single point of failure. The ALB places its own nodes in every AZ you give it, and routes around an AZ that is down.

How to choose a region:
1. **Latency**: close to your users (Frankfurt is close to Serbia).
2. **Law and data residency**: EU data in the EU.
3. **Service availability**: not every service or feature is in every region.
4. **Price**: the same service costs different amounts in different regions.

**In EventPark:**
- Region `eu-central-1`, for all four reasons above.
- VPC with subnets in **2 AZs** (`a` and `b`): the ALB requires it, and API tasks can run in both.
- **RDS and ElastiCache are single-AZ** (ADR-020) to save money. That is an *honest trade-off*: if that AZ fails, the
  database is down. In production you'd pay for **Multi-AZ** (a standby copy in another AZ that takes over
  automatically).
- SQS and S3 are regional, so they are already multi-AZ for free.

**Common mistakes:**
- "An AZ is one data centre." It can be several; what matters is that it's an independent failure domain.
- "Region = HA." Being *in* a region gives you nothing by itself; you have to *spread your resources* across AZs.
- Creating things in the wrong region because the console remembered another one. Your console was on
  `eu-north-1`! Always check the region selector before creating regional resources.

**Test yourself:**
1. If the AZ with our RDS instance fails, what still works and what doesn't?
2. Is a subnet regional or zonal? Why does that matter for the ALB?
3. Why might `eu-central-1a` in your account not be the same building as `eu-central-1a` in mine?

---

## 3. Shared responsibility

**ELI5:** AWS is the landlord of a big apartment building. The landlord locks the front door, keeps the walls
standing and the electricity on. **You** lock your own apartment door, decide who gets a key, and don't leave your
valuables on the balcony. If you leave your door open and get robbed, that's not the landlord's fault.

**Real explanation:**
- **AWS is responsible for security "OF the cloud"**: physical data centres, hardware, the global network, the
  virtualisation layer (hypervisor), and for managed services the software itself (e.g. patching the Postgres
  engine in RDS).
- **You are responsible for security "IN the cloud"**: identities and permissions (IAM), network rules (security
  groups, which subnets are public), data (encryption, whether a bucket is public), secrets, OS patching where you
  manage the OS, and your application code.

The line **moves depending on the service type**:

| | EC2 (IaaS) | RDS (managed) | Fargate (serverless containers) |
|---|---|---|---|
| Physical hardware, hypervisor | AWS | AWS | AWS |
| OS patching | **You** | AWS | AWS (host); **you** (what's inside your image) |
| Database engine patching | **You** | AWS (you choose the maintenance window) | n/a |
| Network rules (security groups) | **You** | **You** | **You** |
| IAM permissions | **You** | **You** | **You** |
| Data encryption settings, backups config | **You** | **You** | **You** |
| Your code and dependencies | **You** | **You** | **You** |

Notice the bottom rows: **IAM, network rules, data and code are always yours.** That's where almost all real
cloud breaches happen: a public S3 bucket, an access key pushed to GitHub, a security group open to the whole
internet (`0.0.0.0/0`) on a database port, an admin account without MFA.

**In EventPark,** your half of the deal, built into the design:
- No long-lived access keys anywhere: `aws login` for you, OIDC for GitHub Actions, IAM roles for ECS tasks.
- Secrets in SSM Parameter Store, never in git (`.env` is gitignored).
- Databases in **private subnets** with no internet route; their security groups only accept traffic from the app's
  security group.
- The ALB only accepts traffic from CloudFront, plus a secret header (ADR-018).
- Least-privilege IAM: each ECS service gets only the permissions it needs (e.g. the API **can't** send to SQS at
  all; only the outbox relay can).
- Container images built `FROM` a slim base, running as a **non-root** user (Phase 1), because what's inside your
  image is your responsibility even on Fargate.

**Common mistakes:**
- "It's on AWS, so it's secure." AWS gives you secure *building blocks*; misconfiguring them is on you.
- "Managed service = AWS handles all security." AWS patches RDS, but **you** decide whether it's publicly reachable.

**Test yourself:**
1. Someone's S3 bucket full of customer data is public and gets scraped. Whose responsibility was it, and why?
2. On Fargate, who patches the host OS? Who fixes a vulnerable Python library inside your image?

---

## 4. Identity: root, IAM users, groups, roles, policies, MFA, credentials

**ELI5:** The **root user** is the master key: it opens every door, including the one marked "sell the building".
You lock it in a safe. For daily work you get a **personal key card** (IAM user) that opens the doors your job
needs. **Groups** are like "all keys for the maintenance team open these doors". **Roles** are visitor badges
that anyone allowed can borrow for a while and must give back. **MFA** means the key card only works together with
a code from your phone, so a stolen card alone is useless.

### 4.1 The pieces

**Authentication vs authorization** (two different questions):
- **Authentication (AuthN)**: *who are you?* (password + MFA, a signed token)
- **Authorization (AuthZ)**: *what are you allowed to do?* (policies)

**Root user**
- The identity created with the account (your sign-up email).
- Can do **everything**, including things no IAM policy can grant or block: closing the account, changing the
  payment method and support plan, restoring IAM access if all admins are locked out.
- **IAM policies cannot restrict root.** (Only Service Control Policies from AWS Organizations can, and we can't use
  Organizations on the Free plan.) So the only protection is: strong password + MFA, and **don't use it**.
- AWS's guidance: use root only for the handful of tasks that require it. You used it to create your IAM user,
  activate IAM billing access and enable root MFA. That's exactly right.

**IAM user**
- A permanent identity for a person (or, in old setups, a program) inside the account.
- Can have a **console password** (for the web console) and/or **access keys** (for API/CLI).
- Yours: `damjan.petrov`, console password + MFA, **no access keys**.

**IAM group**
- A collection of users. Policies attached to the group apply to every member.
- Groups can't sign in themselves; they only organise permissions.
- Yours: `Admins` with `AdministratorAccess`, with you as the member. Manage permissions per *job*, not per person.

**IAM role**
- An identity with permissions but **no password and no long-term keys**. Someone (or something) **assumes** it and
  gets **temporary credentials** from **STS** (Security Token Service).
- Used by: AWS services (an ECS task's role lets your code read SQS), GitHub Actions (via OIDC, Phase 10),
  people from other accounts.
- The 3 roles you saw in IAM are **service-linked roles**: AWS created them so its own services (Support, Trusted
  Advisor…) can work in your account. Leave them alone.

**Policies**
JSON documents that say what is allowed or denied:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sqs:SendMessage"],
      "Resource": "arn:aws:sqs:eu-central-1:493116365697:eventpark-dev-entry-events"
    }
  ]
}
```
- **Effect**: Allow or Deny. **Action**: which API calls. **Resource**: on what (by ARN). Optional
  **Condition**: when (e.g. only with MFA, only from this IP).
- `AdministratorAccess` is the AWS-managed policy `Action: "*", Resource: "*"`: everything.

**How AWS decides (policy evaluation), in three rules:**
1. **Default deny**: if nothing allows it, it's denied.
2. An **explicit Allow** permits it…
3. …unless there's an **explicit Deny** anywhere. **Deny always wins.**

**ARN (Amazon Resource Name)**: the full unique name of anything in AWS:
```
arn:aws:iam::493116365697:user/damjan.petrov
arn : partition : service : region : account-id : resource
```
IAM is global, so the region field is **empty** (that's the `::`). An SQS queue's ARN has `eu-central-1` in it.

**Least privilege**: give every identity only the permissions it actually needs. `AdministratorAccess` for
yourself is acceptable for a learning account (you're the only human); our *application* will get tightly scoped
roles.

### 4.2 MFA, and how the 6-digit code works

- MFA = **something you know** (password) + **something you have** (phone or security key). A stolen password alone
  is no longer enough.
- An authenticator app uses **TOTP** (Time-based One-Time Password, RFC 6238):
  1. When you scanned the QR code, AWS and your phone **shared a secret key** (that's what the QR contains).
  2. Every 30 seconds, both sides compute `HMAC(secret, current_time / 30)` and take 6 digits from it.
  3. Same secret + same time → same code. Nothing is sent over the network; the phone can be offline.
  4. That's why setup asked for **two consecutive codes**: to prove your phone's clock is in sync with AWS.
- Passkeys and security keys (FIDO2/WebAuthn) are even stronger: they're bound to the real website, so a phishing
  site can't reuse them. That's why they're a great backup device.

### 4.3 Credentials: long-lived keys vs short-lived credentials

| | Long-lived access key | Short-lived credentials |
|---|---|---|
| What | Access key ID + secret, stored in a file | Key + secret + **session token**, from STS |
| Key ID prefix | `AKIA…` | `ASIA…` |
| Lifetime | **forever** until you delete it | minutes to hours, then useless |
| If it leaks | attacker has access until someone notices | attacker has a short window at most |
| How you get them | created in IAM, saved in `~/.aws/credentials` | `aws login`, assuming a role, OIDC |

Leaked access keys are the **#1 way AWS accounts get compromised**. Bots scan every public GitHub push for `AKIA…`
strings and start crypto-mining within minutes. That's why you have **no access keys**.

**`aws login`** (AWS CLI ≥ 2.32): the CLI opens a browser page, you sign in with your normal console credentials and
MFA, and the CLI receives **short-lived credentials**, cached under `~/.aws/` (your home directory, not the project,
so they can't be committed). When the sign-in session expires, you just run `aws login` again.

**Preview of later phases:** this "no long-lived keys" idea repeats everywhere:
- ECS tasks get credentials from their **task role** automatically (Phase 12).
- GitHub Actions proves "I am workflow X in repo damjanxp/eventpark" with an **OIDC token** and gets a role's
  temporary credentials (Phase 10/13). No AWS secret is stored in GitHub at all.

### 4.4 App roles ≠ IAM

EventPark has its own **app roles**: ADMIN, EVENT_MANAGER, GATE_OPERATOR, PARKING_STAFF, DRIVER. They are checked
**by our API**, stored in **our database**, and have nothing to do with AWS IAM. IAM only controls **AWS resources**
(which ECS task may read which queue). A gate operator never has an IAM identity. (The original brief mixed these up;
see `kickoff-notes.md`.)

**Common mistakes:**
- Using root "just this once" for daily things.
- Creating an access key "to get the CLI working" (that's what `aws login` replaces).
- Thinking a group is an identity you can sign in as.
- Confusing app roles with IAM roles.

**Test yourself:**
1. Why can't you protect root with an IAM policy, and what protects it instead?
2. A policy allows `s3:*`, another attached policy denies `s3:DeleteObject`. Can the user delete an object?
3. What's in the QR code you scanned for MFA, and why does the code still work with your phone in airplane mode?
4. Your teammate pastes an `AKIA…` string into a public repo. What happens, and what should they do immediately?

---

## 5. Billing: how AWS charges, the Free plan, credits, budgets

**ELI5:** AWS is like a taxi meter that keeps running while anything you created exists, even if you're not using
it. A **budget** is a smoke alarm: it beeps (emails you) when spending goes over a line, but it doesn't put out the
fire. The fire goes out only when you **delete the things that cost money**. Your **Free plan** is like a prepaid
card with no way to overdraw: when the money on it is gone, the card just stops working (the account closes), and
nobody sends you a bill.

### 5.1 How AWS charges

Every service has its own **pricing dimensions**:
- **Time running**: an RDS instance per hour, a Fargate task per second (vCPU + memory), an ALB per hour,
  a public IPv4 address per hour (~$0.005/h each).
- **Usage**: SQS per million requests, S3 per GB stored + per request, CloudWatch Logs per GB ingested.
- **Data transfer**: data *leaving* AWS to the internet, and between AZs, costs money; data coming *in* is free.

Things that surprise beginners:
- **Idle ≠ free.** A database with zero queries still costs its hourly price. A NAT Gateway costs ~$0.05/h doing nothing.
- **Stopped ≠ deleted.** Some resources (disks, snapshots, Elastic IPs) still cost money when "stopped".
- **Prices differ per region.**
- Cost data appears **hours later**, not instantly.

### 5.2 The Free plan and credits (ADR-022)

Since July 2025, new accounts choose:
- **Free plan**: you get **$100 credits + up to $100 more** for completing onboarding activities, valid for
  **6 months**. AWS **does not charge you**. When the credits are used up or 6 months pass, the account is
  **closed** (you can still upgrade within 90 days to keep the data; then it's deleted).
- **Paid plan**: normal pay-as-you-go billing; credits still apply, but anything beyond them is charged to your card.

Actions that **automatically upgrade** you to Paid (and end the remaining free credits): clicking *Upgrade plan*,
creating an **AWS Organization**, and anything that needs one (Control Tower, IAM Identity Center with organization
access). That's why these are forbidden in this project.

**Credits** are applied to your bill: your *usage* still costs $X, and the credits subtract $X. So the **net** cost
you see is $0, while your credits shrink. That's why the budget must **exclude credits** (charge type ≠ Credit):
otherwise it always sees $0 and never alerts.

### 5.3 The cost tools

| Tool | What it does | What it does NOT do |
|---|---|---|
| **Budgets** | Compares actual or forecast cost to a limit; emails you at thresholds (20/50/80/100%) | **Doesn't stop or delete anything.** Updates only a few times a day, so alerts can be hours late |
| **Cost Anomaly Detection** | Machine learning spots unusual spending patterns per service; daily or weekly summary emails | Doesn't stop anything either |
| **Cost Explorer** | Charts and filters of past and forecast costs by service, day, charge type | Needs ~24 h after first opening before data appears |
| **Budget actions** (optional) | Can attach a "deny" IAM policy or stop specific EC2/RDS instances when a threshold is hit | Doesn't cover most resources; not a reliable kill switch |

**Unblended cost** = the actual price of each usage line on the day it happened. (Amortized and blended are for
reserved instances / savings plans across accounts: not relevant for us.)

### 5.4 What actually stops charges

1. **Deleting the resources.** For us: `terraform destroy` at the end of every AWS session. This is the real
   control, and it's why the dev environment is *ephemeral* and fully built by Terraform.
2. **The Free plan**, as the final guarantee: worst case, the account closes. Never a bill.

Budgets and anomaly detection are **early warnings** that credits are burning faster than planned
(e.g. you forgot `terraform destroy` overnight).

**In EventPark:**
- Budgets: `eventpark-monthly` $25, `eventpark-daily` $5. TODO: add the Charge type → Excludes → Credit filter
  once Cost Explorer has data.
- Expected burn: about $2–2.5 per 8-hour AWS day × ~10 AWS days (Phases 10–15) ≈ $25–40 of credits.
- All AWS phases must finish within 6 months of sign-up (October 2026 → early April 2027).
- Cost-driven design decisions you'll see later: no NAT Gateway by default (ADR-012), single-AZ RDS (ADR-020),
  SSM instead of Secrets Manager (ADR-014), Fargate Spot for workers.

**Common mistakes:**
- "I set a budget, so I can't overspend." A budget is an alarm, not a limit.
- "It said $0 on the dashboard, so it's free." Credits hide the real cost. Look at cost *before* credits.
- "I'm not using it, so it's not costing anything." If it exists, it's probably on the meter.

**Test yourself:**
1. Your budget says $0 spent but your credits went from $100 to $91. What happened?
2. You forget to run `terraform destroy` on Friday evening. What alerts you, and how quickly? What limits the damage?
3. Why would creating an AWS Organization be a disaster for this project?

---

## 6. The EventPark architecture, from gate scan to dashboard update

This is checkpoint question 4. Below is the full path; your job is to **compress it into your own one-minute
explanation** (say it out loud, time yourself).

### 6.1 The big idea in one ELI5

The gate is a **bouncer checking a guest list**: the driver is waiting, so the answer must come **right now** and
must be **correct**. So the bouncer (API + database) decides immediately. Everything else (updating the scoreboard,
emails, reports) is written on a **note** and dropped in a **mailbox** (SQS). **Helpers** (workers) empty the
mailbox at their own pace. If a helper is slow or crashes, the bouncer keeps working and the notes wait.

### 6.2 Step by step (pre-booked QR entry)

```
Gate (Pi) ──scan──▶ API ──one transaction──▶ PostgreSQL (decision + outbox row)
   ◀──OPEN──────────┘
                     Outbox relay ──▶ SQS queue ──▶ Worker ──▶ Redis pub/sub ──▶ API ──WebSocket──▶ Dashboard
```

1. **Scan.** The driver shows a QR code. The **gate controller** (our simulated Raspberry Pi) sends
   `POST /gate/v1/entry/scan {scan_id, ticket_code}` to the API over HTTPS.
   - `scan_id` is an **idempotency key**: a unique ID for *this* scan. If the network drops and the Pi retries,
     the API recognises the same `scan_id` and returns the **same answer** instead of letting the car in twice.
2. **Check who's asking.** The API authenticates the device (its API key) and checks a **rate limit** in Redis
   (protection against a broken or malicious device spamming requests).
3. **Decide, in ONE database transaction** in PostgreSQL (the **source of truth**):
   - lock the ticket row (`SELECT … FOR UPDATE`) so two scans of the same QR can't both win,
   - validate: event is live, ticket is valid, this gate is an entry gate of this venue,
   - mark the ticket `ENTERING` with a **60-second hold**, create a **passage** `AUTHORIZED`,
   - insert an **outbox row** (`passage.authorized`): a note saying "this happened",
   - **COMMIT**: all of it happens, or none of it.
4. **Answer.** The API replies `OPEN` (with zone and display text). The Pi opens the barrier. This whole thing
   takes milliseconds.
5. **Car passes.** The **loop detector** sees the car go through; the Pi calls
   `POST /gate/v1/passages/{id}/complete`. Another short transaction: passage `COMPLETED`, ticket `INSIDE`,
   **zone occupancy counter +1**, another outbox row. (Two phases, because "barrier opened" ≠ "car went in": cars
   reverse away. If nothing completes within 60 s, the hold expires. ADR-007.)
6. **Outbox relay.** A separate process repeatedly reads unpublished outbox rows
   (`FOR UPDATE SKIP LOCKED`, so two relays never grab the same rows), sends them to **SQS**, and marks them published.
7. **SQS** (the mailbox) stores the message durably until a worker processes and deletes it. Messages that keep
   failing go to a **dead-letter queue (DLQ)** to be inspected, instead of blocking everything.
8. **Entry worker** receives the message and:
   - updates statistics (`hourly_stats`) in Postgres,
   - refreshes the **occupancy snapshot** in Redis (a fast read model for the dashboard),
   - `PUBLISH`es a realtime message on the Redis channel `realtime:event:{id}`.
   It's **idempotent**: it records which message IDs it has processed, so a duplicate does nothing.
9. **Realtime fan-out.** Every API instance is **subscribed** to that Redis channel. Each one forwards the message
   to the browsers connected **to it** over a **WebSocket** (a connection that stays open so the server can push).
   With 3 API instances behind a load balancer, your browser is connected to just one of them; Redis pub/sub
   makes sure all of them hear every update.
10. **Dashboard updates.** The React dashboard receives the push and the occupancy number changes, about
    **1–2 seconds** after the car passed (**eventually consistent**).

### 6.3 The four rules that make it correct (why it's built this way)

| Rule | Why |
|---|---|
| **Sync decision, async side effects** (ADR-004) | The driver waits only for the decision; slow things (stats, email, S3) can't slow the barrier or break it |
| **Transactional outbox** (ADR-005) | You can't write to Postgres *and* SQS atomically (the **dual-write problem**). If you commit and then the SQS send fails, the event is lost. Writing the note in the *same* transaction means: decision committed ⇔ note exists |
| **Idempotency everywhere** | Networks fail and retry. The relay can send a message twice (crash after sending, before marking). So gate requests carry `scan_id`/`request_id`, and every consumer ignores duplicates |
| **Postgres is the truth, Redis is rebuildable** (ADR-003) | Capacity must never be exceeded, so it's enforced by the database (atomic `UPDATE … WHERE occupied < capacity`, constraints, row locks). If Redis dies, things get slower, never wrong |

### 6.4 Same picture on AWS (preview of Phases 10–14)

Browser / gate → **CloudFront** (HTTPS front door) → **ALB** (load balancer in 2 AZs) → **ECS Fargate** tasks
(API, relay, workers, all one Docker image with different commands) → **RDS PostgreSQL**, **ElastiCache (Valkey)**,
**SQS**, **S3**; logs and alarms in **CloudWatch**; everything created by **Terraform** and deployed by
**GitHub Actions**. Locally the same code runs in Docker Compose against Postgres, Valkey, ElasticMQ (fake SQS) and
SeaweedFS (fake S3). Only environment variables change.

**Test yourself:**
1. Why doesn't the API send the SQS message itself, right after committing?
2. The worker crashes halfway through a message. What happens to the message, and why is a second delivery safe?
3. Two cars scan at two gates at the same moment for the last free place. What guarantees only one gets in?
4. Redis is wiped. What breaks, what doesn't, and how does it recover?

---

## 7. Preparing the "real gate system" section of the journal

**This part is deliberately NOT answered here.** You are the domain expert: you've installed these gates and
I haven't. The value of this section is comparing **your real-world knowledge** with the spec in
`01-product-and-domain.md`. If you read our version first, you'll unconsciously describe our model instead of
reality, and we lose the chance to find where the spec is wrong.

So: write what you **know from work**, including "I don't know" and "it depends on the installation". Short bullet
points are fine.

Vocabulary that helps you describe it (meanings only):
- **Loop detector (induction loop):** a wire loop in the road that senses a car's metal above it. Gates often have
  one *before* the barrier (car present / arming) and one *after* (car has passed / safety: don't close on a car).
- **Anti-passback:** a rule preventing a ticket from being used to enter twice without exiting in between (e.g. passing
  a QR back to a friend).
- **Tailgating:** a second car following closely through a single barrier opening.
- **Fail-closed / fail-open:** what a device does when it can't reach the server: keep the barrier shut, or let
  people through.
- **Offline mode / local whitelist:** the device keeps a local list of valid codes so it can decide without the server.
- **Timeout / retry:** how long the device waits for the server before giving up, and whether it asks again.
- **Heartbeat:** the device regularly saying "I'm alive" to the server, so staff see when a gate goes offline.
- **Manual open / remote open:** an operator opening the barrier without a valid ticket (and whether it's logged).

Questions to think through while writing (these are in the journal template; this is the same list in more detail):
1. A car arrives: what triggers what, in order, until the barrier closes? What's on the display at each step?
2. What does the Pi decide alone, and what does it ask the server? What's in the request? How fast is the answer?
3. What's printed on a drive-up ticket (code type, time, gate, anything else)? Paper jam / empty roll: what happens?
4. Paying at the exit terminal: what happens on a declined card, a terminal that freezes, a timeout?
5. Network drop: does the barrier stay shut? Is there an offline mode? How long until staff notice?
6. Real problems: reversing cars, tailgating, lost tickets, a misfiring loop, the same QR scanned twice, a barrier
   hitting a car.
7. Operators: what can they do remotely? Is every manual action logged, with who and why?
8. Pricing in real installations: free minutes, per started hour, daily cap, lost-ticket fee. What's typical?

---

## Phase 0 checkpoint (answer these in the journal after the review)

1. What's the difference between a region and an AZ, and why does the ALB need two AZs? → §2
2. Why should you never use the root user day-to-day? → §4.1
3. Does a budget stop AWS from charging you? What does? → §5.3–5.4
4. In one minute, explain the EventPark architecture from gate scan to dashboard update. → §6
