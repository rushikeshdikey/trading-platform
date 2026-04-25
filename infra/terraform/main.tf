###############################################################################
# Trading Journal — single-VM AWS deploy.
#
# Architecture:
#   * One EC2 t4g.micro running Docker Compose (app + postgres + caddy).
#   * EBS gp3 root volume holds the OS, app images, and the postgres data dir
#     (data dir is bind-mounted from the host so snapshots back up everything).
#   * Elastic IP attached so the public address never changes — Namecheap DNS
#     points trading.zilionix.com -> this EIP.
#   * SSM Session Manager for ops access (no SSH key required, no port 22 open
#     by default). SSH stays available for the admin IP as an escape hatch.
#
# After `terraform apply` you still need to:
#   1. Build & push the Docker image to GHCR.
#   2. SSH/SSM in, copy /opt/trading/.env from your secrets, set APP_IMAGE,
#      then `docker compose pull && up -d`.
#   3. Add an A record at Namecheap: trading -> <eip_address output>.
###############################################################################

# Use the default VPC + subnets — no NAT gateway, no extra cost. Public-only
# access works because we want the app reachable from the internet anyway.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Latest Amazon Linux 2023 ARM AMI — has dnf + systemd, includes SSM agent
# preinstalled. ARM (Graviton) gives ~20% better $/perf vs x86 t-class.
data "aws_ami" "al2023_arm" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

# -----------------------------------------------------------------------------
# Security group: 80 + 443 open, 22 limited to your IP.
# -----------------------------------------------------------------------------
resource "aws_security_group" "app" {
  name        = "trading-journal-app"
  description = "Trading journal: HTTP/HTTPS public, SSH to admin IP only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "SSH (admin only). SSM Session Manager works without this."
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_ip_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# -----------------------------------------------------------------------------
# IAM role for SSM Session Manager — lets you `aws ssm start-session` instead
# of opening port 22.
# -----------------------------------------------------------------------------
resource "aws_iam_role" "ec2_ssm" {
  name = "trading-journal-ec2-ssm"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.ec2_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2_ssm" {
  name = "trading-journal-ec2-ssm"
  role = aws_iam_role.ec2_ssm.name
}

# -----------------------------------------------------------------------------
# EC2 instance.
# -----------------------------------------------------------------------------
resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023_arm.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_ssm.name

  # SSH key is optional. SSM works without it.
  key_name = var.key_pair_name == "" ? null : var.key_pair_name

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    delete_on_termination = false # keep DB data if instance is replaced
    encrypted             = true
  }

  # IMDSv2 only (mitigates SSRF token leakage).
  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2
  }

  user_data = file("${path.module}/user_data.sh")

  # Replacing the instance would lose the EBS volume and pgdata. Force
  # `terraform taint` if you really mean it.
  lifecycle {
    ignore_changes = [ami, user_data]
  }

  tags = {
    Name = "trading-journal"
  }
}

# -----------------------------------------------------------------------------
# Elastic IP — DNS at Namecheap points here, never changes across instance
# replacements.
# -----------------------------------------------------------------------------
resource "aws_eip" "app" {
  domain = "vpc"
  tags = {
    Name = "trading-journal"
  }
}

resource "aws_eip_association" "app" {
  instance_id   = aws_instance.app.id
  allocation_id = aws_eip.app.id
}
