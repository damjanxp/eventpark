# GitHub Actions -> AWS without stored keys (docs/03 §5).
#
# Each workflow run gets a signed OIDC token from GitHub. STS checks it against this provider and the role's TRUST
# policy ("who may become this role": only this repo's main branch) and hands back credentials valid for an hour.
# The PERMISSION policy below says what the role may then do ("what can I do"), scoped to our resources by ARN.

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # AWS verifies GitHub's certificate chain itself for this provider; the thumbprint is no longer used,
  # but older tooling expects one, so the documented value is kept.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      # Only workflows running on main of this repo. A fork, a PR branch or another repo gets "access denied".
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name                 = "eventpark-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.github_trust.json
  max_session_duration = 3600
}

locals {
  arn_prefix = "arn:aws:%s:${var.region}:${local.account_id}"
  cluster    = "eventpark-dev"
}

# Least privilege: exactly the calls deploy.yml makes (docs/03 §3 "GitHub Actions role"), scoped by ARN wherever the
# service supports it. Where AWS has no resource-level permissions (ecr:GetAuthorizationToken,
# ecs:RegisterTaskDefinition, ecs:Describe*), "*" is the only option, and those actions can't change anything alone.
data "aws_iam_policy_document" "github_deploy" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for r in aws_ecr_repository.this : r.arn]
  }
  statement {
    sid       = "EcsTaskDefinitions"
    actions   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition", "ecs:DescribeClusters", "ecs:DescribeServices", "ecs:DescribeTasks", "ecs:ListServices"]
    resources = ["*"]
  }
  statement {
    sid     = "EcsDeploy"
    actions = ["ecs:UpdateService"]
    resources = [
      "${format(local.arn_prefix, "ecs")}:service/${local.cluster}/*",
    ]
  }
  statement {
    sid       = "EcsOneOffTasks" # migrations: run the backend image with `alembic upgrade head`, wait for it
    actions   = ["ecs:RunTask"]
    resources = ["${format(local.arn_prefix, "ecs")}:task-definition/${local.cluster}-*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["${format(local.arn_prefix, "ecs")}:cluster/${local.cluster}"]
    }
  }
  statement {
    sid       = "PassOnlyOurTaskRoles" # RegisterTaskDefinition/RunTask hand roles to ECS: only ours, only to ECS
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/${local.cluster}-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  statement {
    sid       = "FrontendBucketList"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.cluster}-frontend-${local.account_id}"]
  }
  statement {
    sid       = "FrontendBucketSync" # aws s3 sync dist/ --delete
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${local.cluster}-frontend-${local.account_id}/*"]
  }
  statement {
    sid       = "CdnInvalidate" # CloudFront ARNs have no region; the distribution id is only known after apply
    actions   = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"]
    resources = ["arn:aws:cloudfront::${local.account_id}:distribution/*"]
  }
  statement {
    sid       = "ImageTag" # the deployed tag, so tomorrow's `terraform apply` starts from it (ADR-016)
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = ["${format(local.arn_prefix, "ssm")}:parameter/eventpark/dev/image_tag"]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  name   = "deploy"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}
