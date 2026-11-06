output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "task_subnet_ids" {
  description = "where ECS tasks run: public (default) or private when enable_nat"
  value       = var.enable_nat ? aws_subnet.private[*].id : aws_subnet.public[*].id
}

output "tasks_need_public_ip" {
  value = !var.enable_nat
}

output "sg" {
  value = {
    alb    = aws_security_group.alb.id
    app    = aws_security_group.app.id
    worker = aws_security_group.worker.id
    db     = aws_security_group.db.id
    cache  = aws_security_group.cache.id
  }
}
