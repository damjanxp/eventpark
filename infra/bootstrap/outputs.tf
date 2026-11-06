output "state_bucket" {
  description = "put this in envs/dev/backend.tf and in this stack's backend block"
  value       = aws_s3_bucket.state.bucket
}

output "ecr_repository_urls" {
  value = { for name, repo in aws_ecr_repository.this : name => repo.repository_url }
}

output "github_deploy_role_arn" {
  description = "role-to-assume in .github/workflows/deploy.yml"
  value       = aws_iam_role.github_deploy.arn
}

output "alarm_topic_arn" {
  value = aws_sns_topic.alarms.arn
}
