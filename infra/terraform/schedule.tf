###############################################################################
# Cost-saving on/off schedule for the EC2 instance.
#
# The trading day in IST is ~09:15-15:30. We start the instance at 07:30 IST
# (gives 1 h 45 m for bars to backfill + scanners to pre-warm before market
# open) and stop at 17:30 IST (1 h after market close, captures the 15:35 IST
# EOD pre-warm + a buffer for quick post-close lookups). On weekends the
# instance stays off entirely.
#
#   Daily on-time:  07:30 → 17:30 IST  = 10 h
#   Weekly on-time: 5 × 10 h           = 50 h  (vs. 168 h always-on)
#
# t4g.medium pricing (ap-south-1): $0.0336/h on-demand.
#   Always-on:    ~$24.20/mo (compute) + ~$2.40/mo (EBS gp3 30 GB) ≈ $26.60
#   Scheduled:    ~$7.20/mo (compute)  + ~$2.40/mo (EBS)             ≈ $9.60
#   Savings:      ~$17/mo (~64 %).
#
# (EBS is billed even when the instance is stopped — only compute hours go
# away. The Elastic IP is free while attached to an existing instance, even
# when stopped.)
#
# Manual override: stop/start the instance from the AWS console any time.
# These rules will resume their schedule on the next firing. To pause the
# schedule (e.g. for a weekend analysis session), set
# `schedule_enabled = false` in tfvars and re-apply.
#
# Apply notes:
#   1. `terraform plan` to review the new resources.
#   2. `terraform apply` adds the schedules. They take effect at the next
#      cron firing — they DON'T immediately stop or start the instance.
#   3. After the first scheduled stop, verify in the AWS console that the
#      instance actually stopped (EventBridge Scheduler → execution history).
###############################################################################

variable "schedule_enabled" {
  description = "Master switch — set to false to disable the on/off schedule without removing the resources."
  type        = bool
  default     = true
}

variable "schedule_start_cron" {
  description = "Cron expression for instance START. Default: 02:00 UTC = 07:30 IST mon-fri."
  type        = string
  default     = "cron(0 2 ? * MON-FRI *)"
}

variable "schedule_stop_cron" {
  description = "Cron expression for instance STOP. Default: 12:00 UTC = 17:30 IST mon-fri."
  type        = string
  default     = "cron(0 12 ? * MON-FRI *)"
}

# -----------------------------------------------------------------------------
# IAM role — EventBridge Scheduler assumes this to call EC2 Start/Stop.
# Least-privilege: only Start/Stop on the trading journal instance.
# -----------------------------------------------------------------------------
resource "aws_iam_role" "scheduler" {
  name = "trading-journal-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "trading-journal-scheduler"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ec2:StartInstances",
        "ec2:StopInstances",
      ]
      Resource = aws_instance.app.arn
    }]
  })
}

# -----------------------------------------------------------------------------
# Start schedule — every weekday morning IST.
# Uses EventBridge Scheduler's universal target ("aws-sdk:ec2:startInstances")
# which calls the EC2 API directly. No Lambda or SSM document indirection.
# -----------------------------------------------------------------------------
resource "aws_scheduler_schedule" "start" {
  name        = "trading-journal-start"
  description = "Start the trading journal EC2 every weekday at 07:30 IST."
  state       = var.schedule_enabled ? "ENABLED" : "DISABLED"

  schedule_expression          = var.schedule_start_cron
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF" # fire exactly at the cron mark
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:startInstances"
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      InstanceIds = [aws_instance.app.id]
    })
  }
}

# -----------------------------------------------------------------------------
# Stop schedule — every weekday evening IST.
# -----------------------------------------------------------------------------
resource "aws_scheduler_schedule" "stop" {
  name        = "trading-journal-stop"
  description = "Stop the trading journal EC2 every weekday at 17:30 IST."
  state       = var.schedule_enabled ? "ENABLED" : "DISABLED"

  schedule_expression          = var.schedule_stop_cron
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      InstanceIds = [aws_instance.app.id]
    })
  }
}
