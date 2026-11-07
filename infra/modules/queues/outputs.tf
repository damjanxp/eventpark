output "queue_urls" {
  description = "routing key -> URL; become QUEUE_URL_<KEY> env vars of the ECS tasks"
  value       = { for k, q in aws_sqs_queue.this : k => q.url }
}

output "queue_arns" {
  description = "for least-privilege IAM: each worker may only read its own queue"
  value       = { for k, q in aws_sqs_queue.this : k => q.arn }
}

output "dlq_arns" {
  description = "for the 'poison messages' alarm (any DLQ not empty)"
  value       = { for k, q in aws_sqs_queue.dlq : k => q.arn }
}
