# Where CloudWatch alarms go (Phase 14). Email subscriptions must be confirmed by clicking the link AWS sends.
resource "aws_sns_topic" "alarms" {
  name = "eventpark-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}
