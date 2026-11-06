# VPC 10.20.0.0/16 over two AZs (docs/03 §3 VPC, ADR-012).
#   public  10.20.0.0/24, 10.20.1.0/24   ALB + ECS tasks (public IP, inbound only from the ALB SG)
#   private 10.20.10.0/24, 10.20.11.0/24 RDS + ElastiCache, no route to the internet (unless enable_nat)
# The S3 gateway endpoint (free) keeps S3 traffic inside AWS for both route tables.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # RDS/ElastiCache endpoints are DNS names
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  availability_zone       = local.azs[count.index]
  cidr_block              = cidrsubnet(var.cidr, 8, count.index) # 10.20.0.0/24, 10.20.1.0/24
  map_public_ip_on_launch = false                                # ECS assigns public IPs explicitly (assign_public_ip)
  tags                    = { Name = "${var.name}-public-${local.azs[count.index]}", Tier = "public" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.cidr, 8, 10 + count.index) # 10.20.10.0/24, 10.20.11.0/24
  tags              = { Name = "${var.name}-private-${local.azs[count.index]}", Tier = "private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- optional textbook layout: NAT Gateway (~$0.05/h + $0.05/GB). Off by default (ADR-012). ---
resource "aws_eip" "nat" {
  count  = var.enable_nat ? 1 : 0
  domain = "vpc"
  tags   = { Name = "${var.name}-nat" }
}

resource "aws_nat_gateway" "this" {
  count         = var.enable_nat ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = var.name }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  dynamic "route" {
    for_each = var.enable_nat ? [1] : []
    content {
      cidr_block     = "0.0.0.0/0"
      nat_gateway_id = aws_nat_gateway.this[0].id
    }
  }
  tags = { Name = "${var.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id, aws_route_table.private.id]
  tags              = { Name = "${var.name}-s3" }
}

# --- security groups (docs/03 §3 table). Rules are separate resources so SGs can reference each other. ---
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "ALB: HTTP only from CloudFront's origin-facing IPs"
  vpc_id      = aws_vpc.this.id
}

resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "api + fakepay tasks: 8000 from the ALB and from each other (Service Connect)"
  vpc_id      = aws_vpc.this.id
}

resource "aws_security_group" "worker" {
  name        = "${var.name}-worker"
  description = "workers, relay: no inbound at all"
  vpc_id      = aws_vpc.this.id
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "RDS: 5432 from app + worker"
  vpc_id      = aws_vpc.this.id
}

resource "aws_security_group" "cache" {
  name        = "${var.name}-cache"
  description = "ElastiCache: 6379 from app + worker"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_app" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_ingress_rule" "app_from_app" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_ingress_rule" "app_from_worker" { # workers call fakepay (reconciliation)
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_egress_rule" "all_out" {
  for_each          = { app = aws_security_group.app.id, worker = aws_security_group.worker.id }
  security_group_id = each.value
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1" # ECR, SQS, S3, SSM, CloudWatch are reached over HTTPS on the internet (or the S3 endpoint)
}

resource "aws_vpc_security_group_ingress_rule" "db" {
  for_each                     = { app = aws_security_group.app.id, worker = aws_security_group.worker.id }
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "cache" {
  for_each                     = { app = aws_security_group.app.id, worker = aws_security_group.worker.id }
  security_group_id            = aws_security_group.cache.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}
