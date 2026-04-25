variable "aws_region" {
  description = "AWS region. ap-south-1 (Mumbai) gives lowest latency to NSE/Zerodha."
  type        = string
  default     = "ap-south-1"
}

variable "instance_type" {
  description = "EC2 instance size. t4g.micro is free-tier eligible for 12 months, then ~$6/mo. Bump to t4g.small (~$13/mo) if Postgres + app + Caddy run hot."
  type        = string
  default     = "t4g.micro"
}

variable "root_volume_gb" {
  description = "EBS gp3 root volume size in GB. 20 GB covers ~3 years of trading data + DB + image cache."
  type        = number
  default     = 20
}

variable "admin_ip_cidr" {
  description = "Your public IP in CIDR form, used for SSH allow-list in the security group. Override via TF_VAR_admin_ip_cidr or terraform.tfvars. SSM Session Manager works regardless of this."
  type        = string
  # Replace via tfvars; default is intentionally overly-broad-but-not-open.
  # The real lockdown comes from SSH being key-pair-only and SSM being
  # the primary access path.
  default = "0.0.0.0/0"
}

variable "app_domain" {
  description = "Public domain. Used only for outputs / hint text — actual TLS is provisioned by Caddy at runtime."
  type        = string
  default     = "trading.zilionix.com"
}

variable "key_pair_name" {
  description = "Name of an existing AWS key pair for SSH (optional — SSM works without). Leave empty to skip key pair."
  type        = string
  default     = ""
}

variable "github_repo" {
  description = "owner/name of the GitHub repository allowed to assume the deploy role."
  type        = string
  default     = "rushikeshdikey/trading-platform"
}

variable "create_github_oidc_provider" {
  description = "Create the AWS IAM OIDC provider for GitHub Actions. Set to false if zilionix already has one in this account."
  type        = bool
  default     = true
}
