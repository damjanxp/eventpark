# EventPark

**Cloud platform for event parking**: thousands of vehicles, many gates, live occupancy. Built to learn
production-style cloud architecture on AWS.

> 🚧 Work in progress. See [`docs/progress.md`](docs/progress.md).

- Gate controllers (simulated Raspberry Pis: QR scanner, ticket printer, barrier, loop detector, card terminal) ask the
  API on every scan; capacity is enforced race-free in PostgreSQL; retries are idempotent.
- Pre-booked QR passes (online, simulated payment provider with signed webhooks) and drive-up tickets paid at the exit lane.
- Transactional outbox → SQS → workers for statistics, QR codes, emails, reports; Redis for caching, rate limiting and
  realtime WebSocket fan-out; React dashboard, operator console and booking site.
- Traffic simulator: rush hours, presales, 10,000 vehicles, chaos injection, invariant checking.
- AWS: CloudFront, ALB, ECS Fargate, RDS PostgreSQL, ElastiCache, SQS, S3, EventBridge Scheduler, CloudWatch, all in
  Terraform; CI/CD with GitHub Actions + OIDC.

## Docs
Start at [`docs/README.md`](docs/README.md). AI agents: read [`CLAUDE.md`](CLAUDE.md).
