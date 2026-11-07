# modules/queues

Five standard SQS queues (`entry-events`, `exit-events`, `notifications`, `reports`, `maintenance`), each with a
DLQ (`maxReceiveCount = 5`, 14-day retention), SSE-SQS encryption, 20 s long polling. Same names and visibility
timeouts as the local ElasticMQ config, so workers behave identically locally and on AWS.

Cost: first 1M requests/month free; long polling keeps idle workers to ~3 requests/minute each.
