variable "name_prefix" {
  description = "e.g. eventpark-dev -> eventpark-dev-entry-events"
  type        = string
}

variable "queues" {
  description = "routing key -> settings (same names and timeouts as docker/elasticmq/elasticmq.conf)"
  type = map(object({
    visibility_timeout_seconds = number
  }))
  default = {
    entry-events  = { visibility_timeout_seconds = 30 }
    exit-events   = { visibility_timeout_seconds = 30 }
    notifications = { visibility_timeout_seconds = 60 }
    reports       = { visibility_timeout_seconds = 300 }
    maintenance   = { visibility_timeout_seconds = 60 }
  }
}

variable "max_receive_count" {
  type    = number
  default = 5
}
