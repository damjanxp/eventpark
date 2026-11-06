variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "github_repository" {
  description = "owner/name of the repo whose workflows may deploy (OIDC trust policy)"
  type        = string
  default     = "damjanxp/eventpark"
}

variable "alarm_email" {
  description = "where CloudWatch alarms are sent (you confirm the subscription by email once)"
  type        = string
}

variable "ecr_repositories" {
  type    = set(string)
  default = ["eventpark/backend", "eventpark/fakepay"]
}

variable "keep_images" {
  description = "ECR lifecycle: how many images to keep per repository"
  type        = number
  default     = 15
}
