# 05 — Glossary

Every term: **ELI5** → *precise meaning* → where it shows up in EventPark.

## Architecture & distributed systems

| Term | ELI5 | Precise meaning | In EventPark |
|---|---|---|---|
| **Synchronous / asynchronous** | Phone call vs text message | Caller waits for the result vs hands work off and continues | Gate decisions sync; stats/emails async |
| **Source of truth** | The one notebook that's always right | The authoritative store; everything else is derived | PostgreSQL |
| **Idempotency** | Pressing the lift button 5× = one lift | Repeating an operation has the same effect as doing it once | `scan_id`, `request_id`, payment keys |
| **Idempotency key** | A receipt number for a request | Client-generated unique ID the server uses to detect repeats | `passages.idempotency_key` |
| **At-least-once delivery** | The postman may deliver the same letter twice, never zero times | A message is delivered ≥ 1 times; duplicates possible | SQS standard queues |
| **Race condition** | Two people grab the last cookie at once | Outcome depends on timing of concurrent operations | Last parking space |
| **Lost update** | Two edits; the second silently erases the first | Concurrent read-modify-write where one write overwrites the other | Naive counter in Phase 3 |
| **Transaction / ACID** | All-or-nothing bundle of changes | Atomic, Consistent, Isolated, Durable unit of DB work | Every gate use case |
| **Isolation level** | How much you see of others' unfinished work | READ COMMITTED / REPEATABLE READ / SERIALIZABLE anomaly guarantees | Phase 3 experiments |
| **Row lock / `FOR UPDATE`** | Hand on the paper while writing | Lock rows until transaction end | Ticket on scan |
| **`SKIP LOCKED`** | Take the next free ticket at the deli, skip ones being served | Skip rows locked by others instead of waiting | Outbox relay, sweepers |
| **Deadlock** | Two people each holding one chopstick | Cycle of transactions waiting on each other's locks | Prevented by lock order |
| **State machine** | Board game: only certain moves allowed from each square | Explicit states + allowed transitions | Tickets, passages, payments, events, lanes |
| **Dual-write problem** | Writing in two diaries; one write fails | Can't atomically update two separate systems | DB + SQS |
| **Transactional outbox** | Put the letter in the same envelope as the contract | Store events in the DB transaction; relay publishes later | `outbox` + relay |
| **Eventual consistency** | The scoreboard updates a moment after the goal | Replicas/read models converge after a delay | Dashboard, Redis read model |
| **Read model / CQRS** | A summary sheet made from the full records | Separate query-optimised representation of data | Occupancy hash in Redis |
| **Backpressure** | The queue at the door grows instead of the room overflowing | Mechanism that slows producers / buffers work when consumers lag | SQS between API and workers |
| **Hot row / contention** | Everyone queueing at one cashier | Many transactions updating the same row serialise on its lock | `event_zones` counter |
| **Thundering herd** | Everyone rushes the door when it reopens | Many clients retry/reconnect at the same moment | Jitter in gate retries |
| **Exponential backoff + jitter** | Wait longer after each failed knock, at a slightly random time | Retry delays `base·2^n` plus randomness | Gate client, fakepay webhooks |
| **Fail closed / fail open** | Locked door when the power fails vs unlocked | Default behaviour on failure: deny vs allow | Barrier fails closed; rate limiter fails open |
| **Graceful degradation** | Lights dim instead of going out | Non-critical failures reduce features, not availability | Redis down → slower, still correct |
| **Graceful shutdown** | Finish your sentence before hanging up | On SIGTERM: stop taking work, finish in-flight, exit | Workers on ECS |
| **Heartbeat** | "Still here!" every few seconds | Periodic liveness signal | Gate devices every 5 s |
| **Edge device** | The hands on site; the cloud is the brain | Computing at the physical location, near sensors/actuators | Gate controller / Raspberry Pi |
| **HAL** | Talk to "a barrier", not "pin 17" | Hardware Abstraction Layer: interfaces over hardware | `gatectl/hal` |
| **Anti-passback** | A ticket can't let two cars in | Access-control rule forbidding re-entry without exit | Ticket state machine |
| **Webhook** | The bank calls you back | Server-to-server HTTP callback on an event | fakepay → `/webhooks/fakepay` |
| **HMAC signature** | A wax seal only you and the bank can make | Keyed hash proving authenticity + integrity | Webhook verification |
| **Reconciliation** | Comparing your notebook with the bank statement | Periodically resolving state with an external system | Payments `UNKNOWN` |
| **Little's Law** | Cars in the queue = arrivals per second × seconds each stays | `L = λW` | Lane capacity analysis |
| **Percentiles (p50/p95/p99)** | How long the slowest 5% / 1% waited | Value below which X% of observations fall | Latency targets |
| **SLI / SLO** | What you measure / the promise you make | Indicator (e.g. latency) / objective (99% < 300 ms) | Phase 14 |

## Security & identity

| Term | ELI5 | Precise meaning | In EventPark |
|---|---|---|---|
| **Authentication (401)** | Who are you? | Verifying identity | Login, device keys |
| **Authorization (403)** | Are you allowed? | Verifying permission | RBAC |
| **RBAC** | Badge colour decides which doors open | Role-Based Access Control | ADMIN, EVENT_MANAGER, … |
| **JWT** | A stamped visitor badge anyone can read, nobody can forge | Signed JSON token: header.payload.signature | Access tokens |
| **Refresh token** | A voucher to get a new badge | Long-lived, revocable credential to obtain new access tokens | HttpOnly cookie, rotated |
| **Argon2id** | A fingerprint that takes effort to fake | Memory-hard salted password hash | Passwords, device keys |
| **XSS** | A bad script sneaks into your page | Cross-site scripting: injected JS runs in your origin | Why tokens aren't in localStorage |
| **CSRF** | Another site makes your browser click for you | Cross-site request forgery using ambient cookies | `SameSite=Strict` |
| **CORS** | Browser rule about which websites may call your API | Cross-Origin Resource Sharing headers | Avoided via same-origin; S3 presigned PUT needs it |
| **Presigned URL** | A temporary key to one locker | URL signed with credentials, method, key and expiry | Uploads/downloads to S3 |
| **OIDC** | Showing an ID card from a trusted issuer | OpenID Connect: identity tokens from an identity provider | GitHub → AWS; Cognito |
| **Cognito** | The government's passport office instead of your own | AWS managed user directory + OIDC token issuer | Phase 15 |
| **JWKS** | The public stamps to check passports | JSON Web Key Set: public keys to verify JWT signatures | Cognito token verification |

## AWS

| Term | ELI5 | Precise meaning | In EventPark |
|---|---|---|---|
| **Region / AZ** | City / building in the city | Geographic area / isolated data-centre group | eu-central-1, 2 AZs |
| **IAM** | Who may do what to which AWS thing | Identity & Access Management: principals, policies, roles | Task roles, CI role |
| **IAM role** | A hat you put on to get certain permissions temporarily | Identity with policies, assumed via STS for temporary credentials | ECS task/execution roles |
| **Trust policy** | Who is allowed to wear the hat | Policy on a role defining principals that may assume it | GitHub OIDC trust |
| **Least privilege** | Give the key to one room, not the whole building | Grant only the minimum permissions required | Per-worker queue access |
| **STS** | The desk that hands out temporary badges | Security Token Service: temporary credentials | Assume role |
| **VPC** | Your own fenced private network | Isolated virtual network with subnets, routes | `10.20.0.0/16` |
| **CIDR** | A street and its house-number range | IP range notation `a.b.c.d/n` | Subnet sizing |
| **Subnet (public/private)** | A block of houses with / without a road to the highway | Subnet whose route table does / doesn't point to an IGW | ALB+tasks / RDS+cache |
| **Internet Gateway** | The highway on-ramp | VPC component enabling internet routing | Public subnets |
| **NAT Gateway** | A one-way door: out yes, in no | Managed outbound-only internet access for private subnets | Optional (`enable_nat`) |
| **Security Group** | A bouncer at each door who remembers who left | Stateful allow-list firewall on network interfaces | `alb`, `app`, `worker`, `db`, `cache` |
| **ECR** | A shelf for your container images | Elastic Container Registry | `eventpark/backend` |
| **ECS** | A manager who keeps N copies of your container running | Elastic Container Service: clusters, task defs, services | All backend processes |
| **Fargate / Fargate Spot** | Rent containers without seeing servers / cheaper leftovers that can be taken back | Serverless compute for ECS / interruptible discounted capacity | API on Fargate, workers on Spot |
| **Task definition / task / service** | Recipe / one dish / chef keeping N dishes ready | Blueprint / running instance / long-running controller | Per component |
| **Execution role vs task role** | ECS's own key vs your app's key | Role ECS uses to start the task vs role the code uses | Pull image & secrets vs SQS/S3 |
| **Service Connect** | An internal phone book for services | ECS-managed service discovery + proxy | `api` ↔ `fakepay` |
| **ECS Exec** | A remote shell into a running container | SSM-based `execute-command` into tasks | Debug + psql |
| **ALB** | A receptionist spreading visitors over desks | Application Load Balancer (HTTP-level routing, health checks) | api, fakepay |
| **Target group / health check** | The list of desks, and checking if each clerk is awake | Set of targets + probe used to route only to healthy ones | `/readyz` |
| **CloudFront** | Copies of your shop all over the world + one front door | CDN with origins, behaviours, TLS | Single entry point |
| **OAC** | Only the delivery van has the key to the warehouse | Origin Access Control: CloudFront-only access to S3 | Frontend bucket |
| **S3** | Infinite, reliable locker room | Object storage: buckets, keys, objects | Frontend, QR, reports, documents |
| **SQS / DLQ** | Mailbox / box for problem letters | Managed message queue / dead-letter queue | 5 queues + DLQs |
| **Visibility timeout** | "I'm reading this letter; hide it from others for 30 s" | Period a received message is hidden from other consumers | Per queue |
| **Long polling** | Wait at the mailbox up to 20 s instead of checking every second | `WaitTimeSeconds` on ReceiveMessage | Workers |
| **RDS** | PostgreSQL someone else maintains | Managed relational database service | Postgres 17 |
| **Multi-AZ** | A twin database in another building, ready to take over | Synchronous standby for failover | Not used (cost); explained |
| **ElastiCache / Valkey** | A shared super-fast notepad | Managed in-memory store; Valkey = open-source Redis fork | Cache, rate limit, pub/sub |
| **SSM Parameter Store** | A locked drawer for settings and secrets | Hierarchical config/secret storage, KMS-encrypted SecureStrings | DB password, JWT secret |
| **KMS** | The master locksmith | Key Management Service for encryption keys | SSM, S3, RDS encryption |
| **EventBridge Scheduler** | An alarm clock that sends messages | Managed scheduler invoking targets on a schedule | 1-minute tick |
| **CloudWatch Logs / Metrics / Alarms** | Diary / gauges / alarm bells | Log storage, time-series metrics, threshold alerts | Observability |
| **EMF** | Writing a gauge reading into the diary in a special format | Embedded Metric Format: logs that become metrics | Custom metrics |
| **Logs Insights** | Search engine for your diaries | Query language over CloudWatch Logs | Tracing request_id |
| **SNS** | A loudspeaker announcing to subscribers | Pub/sub notification service | Alarm emails |
| **Target tracking autoscaling** | A thermostat for the number of containers | Adjust capacity to keep a metric at a target | API & workers |
| **Credits / Free plan** | Gift card for AWS | New-account promo credits; 6-month free plan | ~$200 budget |

## Tooling

| Term | ELI5 | Precise meaning | In EventPark |
|---|---|---|---|
| **Docker image / container** | Frozen lunchbox / opened lunchbox | Layered filesystem + metadata / isolated running process | Every service |
| **Docker Compose** | A script that starts all lunchboxes together on a shared network | Multi-container local orchestration | Local stack |
| **Terraform** | A recipe for infrastructure | Declarative IaC tool (plan/apply/state) | `infra/` |
| **Terraform state** | Terraform's memory of what it built | Mapping of config to real resource IDs | S3 backend |
| **Terraform module** | A reusable recipe section | Directory of resources with inputs/outputs | `modules/ecs-service` |
| **CI / CD** | Robot that checks / robot that ships | Continuous Integration / Continuous Delivery-Deployment | GitHub Actions |
| **Alembic migration** | Numbered instructions for changing the DB layout | Versioned schema change scripts | `backend/migrations` |
| **OpenAPI** | The API's instruction manual, machine-readable | Spec of endpoints/schemas | Generates the TS client |
| **TanStack Query** | A smart cache of server data in the browser | Server-state fetching/caching library | Frontend data |
| **WebSocket** | An open phone line between browser and server | Full-duplex persistent connection | Live dashboard |
| **testcontainers** | Spin up a real database just for the test | Library starting Docker containers from tests | Integration tests |
| **moto** | A pretend AWS inside your test | Python library mocking AWS APIs | S3/SQS tests |
