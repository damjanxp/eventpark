# SQS: one standard queue per routing key + its dead-letter queue (docs/03 §3 SQS, docs/02 §6).
# Mirrors docker/elasticmq/elasticmq.conf, so local and AWS behave the same ("same code, different config").
#
#   message fails 5 times (maxReceiveCount) -> moved to <name>-dlq, kept 14 days for a human to inspect
#   long polling (20 s): a receive waits for a message instead of returning empty -> fewer billed requests

resource "aws_sqs_queue" "dlq" {
  for_each                  = var.queues
  name                      = "${var.name_prefix}-${each.key}-dlq"
  message_retention_seconds = 14 * 24 * 3600 # maximum: time to notice and investigate a poison message
  sqs_managed_sse_enabled   = true           # encryption at rest with an SQS-owned key: free
}

resource "aws_sqs_queue" "this" {
  for_each                   = var.queues
  name                       = "${var.name_prefix}-${each.key}"
  visibility_timeout_seconds = each.value.visibility_timeout_seconds # > the slowest handler (reports extend it)
  receive_wait_time_seconds  = 20                                    # long polling
  message_retention_seconds  = 4 * 24 * 3600
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = var.max_receive_count
  })
}

# Only our own queues may use a DLQ as their dead-letter target.
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  for_each  = var.queues
  queue_url = aws_sqs_queue.dlq[each.key].id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.this[each.key].arn]
  })
}
