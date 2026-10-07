# Kickoff notes: decisions from the planning chat (2026-10-04)

This file carries over the planning conversation (claude.ai, AWS Project) so any agent working in this repo knows
**what Damjan decided and why**, without that chat. The spec in `docs/01–05` already reflects all of it.

## How the project got here
- Started as "Vehicle Intelligence" (YOLO vehicle detection platform). **Switched to the event-parking platform**
  because Damjan works in access control (barriers, ticket dispensers, QR scanners) and knows the domain from real installations.
- Original parking brief asked for: events, zones, gates, tickets/QR, entry/exit, reservations, automatic space
  assignment, dashboard, SQS queues (entry-events, exit-events, notifications, reports), Redis, ECS behind an ALB,
  CloudWatch, Terraform, GitHub Actions, a 10,000-car traffic simulator. Its instruction stands: **"Do not simplify the
  architecture just to make implementation easier."**
- Correction made to the brief: it listed app roles under "IAM". App roles are app-level RBAC; IAM is for AWS resources.

## Damjan's answers (planning Q&A)
| Topic | Decision |
|---|---|
| Backend | Python + FastAPI |
| Frontend | React + TypeScript + Vite |
| Auth | Own JWT auth first, migrate to Cognito later (Phase 15) |
| AWS budget | No account yet, keep it near $0 → full architecture, **up only while working** (apply/destroy each session), ~$200 new-account credits |
| Setup | Windows with Docker Desktop + WSL2, comfortable with Git/GitHub, has used Docker, has an NVIDIA GPU |
| Time | Full-time sprint, ~1 month (spec is ~28 days → cut list in `04-learning-path.md`) |
| Spec format | Both: markdown in repo (source of truth) + a Claude Doc reading copy |
| Ticket model | Both pre-booked online QR passes **and** drive-up printed tickets |
| Allocation | Zone counting + exact numbered spaces for VIP/reserved |
| Payments | Simulated payment provider (fakepay) |
| Extras in scope | Live dashboard via WebSockets; public driver booking site |
| Extras out of scope (stretch) | Running on a real Raspberry Pi; YOLO plate recognition |

## How Damjan's real gate system works (from his job): basis for the gate design
- QR scanner (for online tickets) and a ticket printer (drive-up), both connected to a **Raspberry Pi** that decides
  what to print and whether a QR code is valid.
- The Pi **asks the server on every scan** (online validation, no local cache).
- Printed tickets are **paid at the exit lane terminal**; pre-booked QR tickets are also scanned at the exit.
→ Modelled as the `gate-controller` service (HAL: scanner, printer, barrier, loop detector, card terminal), opaque ticket
codes, online validation per scan (ADR-006), pay at exit lane (synchronous terminal payment).

## Open items
- [x] Confirm drive-up pricing defaults (15 free minutes, 300 RSD per started hour, 1,500 RSD daily cap) against real installations.
      Damjan: "Prices are okay" (2026-10-07).
- [x] Phase 0 journal: Damjan writes down how the real gate system behaves (incl. network failures, reversing cars,
      lost tickets); then adjust `01-product-and-domain.md` where reality differs. Done 2026-10-07: arming loop,
      ticket-taken sensor, printer paper status, manual exit "released unpaid", safety loop / tailgating.
- [ ] Verify at work (Damjan was unsure): does the Pi ask the server on **every** scan (ADR-006)? What happens on a
      network drop (barrier stays shut? offline whitelist?).
- Stance (Damjan, 2026-10-07): EventPark is **inspired by** the real system, not a replica. Domain details may be
      simplified or invented when reality is unknown. This does **not** relax the architecture (CLAUDE.md §2).
- [x] Decide repo location: **WSL2 filesystem** (`~/projects/eventpark` in Ubuntu-26.04), decided 2026-10-04.
- [x] Create AWS account (**Free plan, never upgrade**: zero spend is a hard requirement, ADR-022) + budgets (Phase 0), done 2026-10-05.
      Budgets still count credits until the Charge type filter is added (Cost Explorer data needed first).

## Facts checked during planning (Oct 2026)
- AWS new accounts (since July 2025): $100 credits + up to $100 more, free plan for 6 months or until credits run out.
- LocalStack Community requires an account/auth token since March 2026; MinIO stopped community Docker images (Oct 2025)
  and was archived → local emulators are ElasticMQ (SQS) + SeaweedFS (S3), moto in tests (ADR-015).
