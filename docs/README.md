# EventPark — documentation

EventPark is a cloud platform for event parking: many gates and thousands of cars. You are building
it to **learn cloud architecture and AWS by doing**, so these docs are both a **specification**
(what to build) and a **course** (what to understand while building it).

## How the docs fit together

| File | What it is | When to read it |
|---|---|---|
| [01-product-and-domain.md](01-product-and-domain.md) | What the system does: actors, tickets, gates, capacity, payments, business rules, state machines | Before Phase 2, and whenever a rule is unclear |
| [02-architecture.md](02-architecture.md) | How it's built: components, request flows, sync vs async, data model, API, Redis, realtime | Before Phase 1; reference during Phases 2–9 |
| [03-aws-and-infrastructure.md](03-aws-and-infrastructure.md) | AWS design: network, services, IAM, Terraform layout, CI/CD, costs, monitoring | Before Phase 10; reference during Phases 10–15 |
| [04-learning-path.md](04-learning-path.md) | **The plan you follow.** 16 phases, each with concepts (ELI5 + deep), tasks, YOU WRITE parts, checkpoints | Every day |
| [05-glossary.md](05-glossary.md) | Every term with an ELI5 and a precise definition | Whenever a word is unfamiliar |
| [decisions.md](decisions.md) | Architecture Decision Records: why we chose X over Y | When you wonder "why not just…?" |
| [progress.md](progress.md) | Phase checklist + where you are now | Start and end of each session |
| `journal/` | **Your** notes: your explanations before building, answers to checkpoints | You write here every phase |
| `study/` | Study notes per phase: every concept explained in depth (ELI5 + real + self-test questions) | Before writing the phase's journal |

## How to use them with an AI agent

1. Open `progress.md` and find the active phase.
2. Tell the agent: *"We're on Phase N. Start the phase."* It reads `CLAUDE.md`, explains the
   concepts, asks you to write your understanding in `journal/phase-NN.md`, and then you build the
   phase together. The tasks marked **YOU WRITE** are yours to code.
3. End the phase with the checkpoint questions, then tick it off in `progress.md`.

## The one-paragraph architecture

Gate controllers (simulated Raspberry Pis) call a **FastAPI** backend on every scan. The backend decides
in one **PostgreSQL** transaction whether to open the barrier, and writes an event to an **outbox**
table in the same transaction. An **outbox relay** publishes those events to **SQS** queues.
**Workers** consume them to update statistics, push live updates (via **Redis** pub/sub → WebSockets)
to the **React** dashboard, generate QR codes and reports into **S3**, and send emails. A **fake payment
provider** handles exit-lane card payments and online bookings. A **traffic simulator** throws
thousands of cars at it. On AWS: **CloudFront → ALB → ECS Fargate**, with **RDS**, **ElastiCache**,
**SQS**, **S3** and **CloudWatch**, all created by **Terraform** and deployed by **GitHub Actions**.
