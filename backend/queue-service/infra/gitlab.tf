# GitLab logs in to AWS with OIDC (no AWS keys stored in GitLab). Only pipelines on
# var.gitlab_deploy_branch of var.gitlab_project_path can assume the deploy role.

resource "aws_iam_openid_connect_provider" "gitlab" {
  count          = var.create_gitlab_oidc_provider ? 1 : 0
  url            = "https://gitlab.com"
  client_id_list = [var.gitlab_oidc_audience]
}

locals {
  gitlab_oidc_arn = (
    var.create_gitlab_oidc_provider
    ? aws_iam_openid_connect_provider.gitlab[0].arn
    : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/gitlab.com"
  )
}

data "aws_iam_policy_document" "gitlab_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.gitlab_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "gitlab.com:aud"
      values   = [var.gitlab_oidc_audience]
    }
    condition {
      test     = "StringLike"
      variable = "gitlab.com:sub"
      values   = ["project_path:${var.gitlab_project_path}:ref_type:branch:ref:${var.gitlab_deploy_branch}"]
    }
  }
}

resource "aws_iam_role" "gitlab_deploy" {
  name               = "${local.name}-gitlab-deploy"
  assume_role_policy = data.aws_iam_policy_document.gitlab_assume.json
}

# What ci/deploy.sh does: copy task definitions with a new image, run the
# migration, update the services, and wait.
data "aws_iam_policy_document" "gitlab_deploy" {
  statement {
    actions = [
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:DescribeTasks",
      "ecs:DescribeServices",
    ]
    resources = ["*"] # these actions don't support resource-level restrictions
  }
  statement {
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/queue-migrate-${var.env}:*"]
  }
  statement {
    actions   = ["ecs:UpdateService"]
    resources = ["arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:service/${data.aws_ecs_cluster.shared.cluster_name}/queue-*"] # the cluster is shared: our services only
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
  }
}

resource "aws_iam_role_policy" "gitlab_deploy" {
  name   = "deploy-queue-service"
  role   = aws_iam_role.gitlab_deploy.id
  policy = data.aws_iam_policy_document.gitlab_deploy.json
}
