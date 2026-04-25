output "eip_address" {
  description = "Public IP. Add an A record at Namecheap: trading -> this value."
  value       = aws_eip.app.public_ip
}

output "instance_id" {
  description = "EC2 instance ID. Use for SSM: aws ssm start-session --target <id>"
  value       = aws_instance.app.id
}

output "ssm_session_command" {
  description = "Copy-paste to open a shell on the instance (no SSH needed)."
  value       = "aws ssm start-session --target ${aws_instance.app.id} --region ${var.aws_region}"
}

output "next_steps" {
  description = "Bootstrap checklist after first apply."
  value       = <<-EOT

    1. DNS: point ${var.app_domain} (A record) -> ${aws_eip.app.public_ip}
    2. SSM in:  aws ssm start-session --target ${aws_instance.app.id} --region ${var.aws_region}
    3. On the box: cd /opt/trading && cp /tmp/.env.template .env (then fill secrets)
    4. Upload Caddyfile + docker-compose.yml + .env via the deploy script.
    5. systemctl start trading
    6. Watch:  sudo docker compose logs -f
  EOT
}
