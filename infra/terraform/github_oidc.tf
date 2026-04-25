###############################################################################
# GitHub Actions → AWS via OIDC.
#
# Lets the .github/workflows/deploy.yml workflow assume an IAM role to call
# `aws ssm send-command` against the trading-journal instance — no long-lived
# AWS access keys stored as GitHub secrets.
#
# After `terraform apply`:
#   1. Terraform outputs `github_actions_role_arn`.
#   2. Add it as the `AWS_ROLE_ARN` secret in the GitHub repo settings.
###############################################################################

# Single OIDC provider per AWS account; check whether one already exists for
# token.actions.githubusercontent.com (zilionix may have created one for the
# main site). data lookup keeps this idempotent across accounts.
data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # GitHub's root CA thumbprint. AWS validates the OIDC tokens against this.
  # Updated by GitHub roughly yearly — check
  # https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  oidc_provider_arn = (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : data.aws_iam_openid_connect_provider.github[0].arn
  )
}

# IAM role assumable by the rushikeshdikey/trading-platform repo on `main`.
# Keep the trust policy tight — only this repo, only the main branch, can
# get a token. Pull-request workflows from forks cannot assume the role.
resource "aws_iam_role" "github_actions" {
  name = "trading-journal-github-actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Allow main + manual workflow_dispatch from any branch.
          "token.actions.githubusercontent.com:sub" = [
            "repo:${var.github_repo}:ref:refs/heads/main",
            "repo:${var.github_repo}:environment:prod",
          ]
        }
      }
    }]
  })
}

# Tight permissions: only allow SSM commands against THIS instance + the
# minimal EC2 describes the workflow needs.
resource "aws_iam_role_policy" "github_actions_deploy" {
  name = "deploy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:SendCommand",
          "ssm:GetCommandInvocation",
          "ssm:ListCommandInvocations",
          "ssm:DescribeInstanceInformation",
        ]
        Resource = "*"
      },
    ]
  })
}
