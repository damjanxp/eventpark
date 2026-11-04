# Experiments: the async backbone (Phase 6)

All on the local stack (`make up`, 2026-11-04). Queues: ElasticMQ; files: SeaweedFS; email: Mailpit.

## 1. Stop the relay during traffic; kill the stats worker

```
docker compose stop outbox-relay worker-entry
5 drive-up cars enter at lane S1                 -> 5/5 OPEN, completed (the gate never waits for the async side)
outbox rows waiting                              -> 10 (passage.authorized + passage.completed per car)
docker compose start outbox-relay                -> outbox empty again within ~1 s
hourly_stats entries                             -> unchanged (4): worker-entry is down, the messages wait in SQS
docker compose start worker-entry                -> entries 4 -> 9: every car counted exactly once
```

**Why it works:** the API only writes outbox rows in its own transaction (no network). The relay catches up from
the table whenever it runs. SQS holds messages until a consumer deletes them. A stopped worker just means a backlog
that drains later (and on AWS, the backlog is the metric that scales the workers out).

## 2. The same message twice → stats unchanged

`tests/integration/test_async.py::test_duplicate_message_is_applied_once`: the second delivery finds its
`(consumer, message_id)` in `processed_messages` and is skipped. Because that row is inserted in the **same
transaction** as the stats update, there is no moment where one happened without the other.

## 3. A message that always fails → DLQ

`tests/integration/test_async.py::test_failed_message_is_not_deleted_and_ends_in_the_dlq` (moto, visibility timeout
0 to make it fast): 5 failed receives, never deleted, then SQS moves it to `<queue>-dlq` because of the
`RedrivePolicy` (`maxReceiveCount = 5`). Nothing was recorded in `processed_messages`, so after fixing the bug the
message can be moved back ("redrive") and will be processed normally.
Locally with ElasticMQ the same happens, only slower (visibility timeout 30 s per attempt ≈ 2.5 min).
The ElasticMQ web UI moved to a separate image in 1.7, so inspect the DLQ with the AWS CLI or the smoke script's
boto3 calls instead of `localhost:9325`.

## 4. Two relays at once

`test_two_relays_in_parallel_never_send_a_row_twice`: 250 rows, two relays in parallel, 250 messages in the queue,
each exactly once. `FOR UPDATE SKIP LOCKED` gives each relay rows the other isn't holding.
