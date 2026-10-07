# Progress

**Active phase:** Phase 1 — Local walking skeleton + CI
**AWS dev environment:** ⛔ down   *(update whenever you run aws-up / aws-down)*

| # | Phase | Est. days | Status | Journal | Notes |
|---|---|---|---|---|---|
| 0 | Foundations & safe AWS account | 1 | ✅ | [phase-00](journal/phase-00.md) | budget Credit filter still TODO |
| 1 | Local walking skeleton + CI | 1.5 | ⬜ | | |
| 2 | Domain model, auth & admin API | 2 | ⬜ | | |
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

## Spending log
<!-- Monday check of Cost Explorer: date — month-to-date cost (before credits) — credits left -->
