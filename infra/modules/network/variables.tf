variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "enable_nat" {
  description = "textbook layout: tasks in private subnets behind a NAT Gateway (~$1.20/day + traffic). Default off."
  type        = bool
  default     = false
}
