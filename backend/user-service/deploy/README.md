# Deploying user-service to AWS

The GitLab pipeline (`.gitlab-ci.yml`) deploys every commit on `main` to ECS on Fargate:

1. **build-and-push** builds the `runtime` image and pushes it to ECR as `:<commit SHA>` and `:latest`.
2. **migrate** ([`scripts/ci/ecs-migrate.sh`](../scripts/ci/ecs-migrate.sh)):
   - fills in [`task-definition.json`](task-definition.json) and registers it as a new revision;
   - runs `alembic upgrade head` once as a one-off task with that revision;
   - fails unless the task exits with code 0.
3. **deploy** ([`scripts/ci/ecs-deploy.sh`](../scripts/ci/ecs-deploy.sh)):
   - points the ECS service at the new revision and waits until the service is stable;
   - fails if ECS rolled back to the previous version.

GitLab signs in to AWS with **OIDC**: each job gets a short-lived token, and AWS lets it assume an IAM role only for this project's `main` branch. No AWS keys are stored in GitLab.

This page is the one-time setup. Everything is done in the AWS console and GitLab settings. Replace `<ACCOUNT_ID>` with your AWS account ID; the region is `us-east-1` throughout.

---

## 1. Network prerequisites

The ECS tasks run in **private subnets**, but still need outbound access to ECR (pull image), CloudWatch Logs, Secrets Manager and Cognito (`AdminGetUser` etc.). Provide one of:
- a **NAT gateway** for the private subnets (simplest), or
- **VPC endpoints**: `ecr.api`, `ecr.dkr`, `s3` (gateway), `logs`, `secretsmanager`, `cognito-idp`.

Security groups:

| Security group | Inbound rule |
|---|---|
| `user-service` (the tasks) | TCP 8000 from the internal ALB's security group (and other services' groups, for `/internal/v1`) |
| RDS | TCP 3306 from the `user-service` group (and each other service's group for its own DB) |

## 2. RDS database and credentials

On the shared RDS MySQL instance:

```sql
CREATE DATABASE db_user CHARACTER SET utf8mb4;
CREATE USER 'user_service'@'%' IDENTIFIED BY '<strong password>';
GRANT ALL PRIVILEGES ON db_user.* TO 'user_service'@'%';
```

Store the credentials in **Secrets Manager** as a secret whose JSON has the keys `username` and `password`. The task definition reads exactly those two keys:

```json
{ "username": "user_service", "password": "<strong password>" }
```

Note the secret's ARN.

## 3. ECR repository and log group

- **ECR → Create repository:** `user-service`, private, mutable tags (the pipeline re-tags `latest`). Note its URI, e.g. `<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/user-service`.
- **CloudWatch Logs → Create log group:** `/ecs/user-service`.

## 4. IAM roles used by the tasks

**Task execution role** (`user-service-execution`): used by ECS to start the container.
- Trusted entity: `ecs-tasks.amazonaws.com`.
- Attach the managed policy `AmazonECSTaskExecutionRolePolicy` (ECR pull, CloudWatch logs).
- Add an inline policy so it can read the DB secret:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "<DB secret ARN>"
    }]
  }
  ```

**Task role** (`user-service-task`): what the application code may do.
- Trusted entity: `ecs-tasks.amazonaws.com`.
- Inline policy:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "cognito-idp:AdminGetUser",
        "cognito-idp:AdminAddUserToGroup",
        "cognito-idp:AdminUpdateUserAttributes"
      ],
      "Resource": "arn:aws:cognito-idp:us-east-1:<ACCOUNT_ID>:userpool/us-east-1_2g8EYCEa1"
    }]
  }
  ```

## 5. GitLab OIDC: let the pipeline sign in to AWS

**IAM → Identity providers → Add provider** (once per AWS account; other services reuse it):
- Type: OpenID Connect
- Provider URL: `https://gitlab.com`
- Audience: `https://gitlab.com`

**IAM → Roles → Create role** `gitlab-user-service-deploy`, with this **trust policy**. The `sub` condition is what restricts it to this project's `main` branch:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/gitlab.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "gitlab.com:aud": "https://gitlab.com",
        "gitlab.com:sub": "project_path:cs302-2026/g2-team5/backend/user-service:ref_type:branch:ref:main"
      }
    }
  }]
}
```

Permissions policy for the same role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "EcrLogin", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    {
      "Sid": "EcrPush",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"
      ],
      "Resource": "arn:aws:ecr:us-east-1:<ACCOUNT_ID>:repository/user-service"
    },
    {
      "Sid": "EcsDeploy",
      "Effect": "Allow",
      "Action": [
        "ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition",
        "ecs:RunTask", "ecs:DescribeTasks",
        "ecs:UpdateService", "ecs:DescribeServices"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PassTaskRoles",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::<ACCOUNT_ID>:role/user-service-execution",
        "arn:aws:iam::<ACCOUNT_ID>:role/user-service-task"
      ],
      "Condition": { "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" } }
    }
  ]
}
```

`EcsDeploy` uses `"*"` because registering a task definition can't be limited by resource. You can tighten `RunTask`, `UpdateService` and `DescribeServices` to the cluster and service ARNs once they exist.

## 6. GitLab CI/CD variables

**GitLab → Settings → CI/CD → Variables.** For each variable:
- tick **Protect variable**, so only `main` sees it;
- set **Environment scope** to `production`, so only the deploy jobs see it.

None of these is a password, so masking is optional.

| Variable | Example |
|---|---|
| `AWS_REGION` | `us-east-1` |
| `AWS_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/gitlab-user-service-deploy` |
| `ECR_REPOSITORY_URI` | `<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/user-service` |
| `ECS_CLUSTER` | `ticketto` |
| `ECS_SERVICE` | `user-service` |
| `ECS_WORKER_SERVICE` | `user-service-worker` |
| `ECS_SUBNETS` | `subnet-aaa,subnet-bbb` (private subnets, comma-separated, no spaces) |
| `ECS_SECURITY_GROUP` | `sg-...` (the `user-service` group) |
| `ECS_EXECUTION_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/user-service-execution` |
| `ECS_TASK_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/user-service-task` |
| `RDS_ENDPOINT` | `<instance>.<id>.us-east-1.rds.amazonaws.com` |
| `DB_SECRET_ARN` | `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:...` |
| `RABBITMQ_SECRET_ARN` | `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:rabibi/service/user-service-...` (created by rabbitmq-service's OpenTofu) |
| `APP_COGNITO_USER_POOL_ID` | `us-east-1_2g8EYCEa1` |

Make `main` a protected branch (it is by default), or protected variables won't reach it.

The names deliberately don't reuse the app's own variable names (`DB_HOST`, `COGNITO_USER_POOL_ID`). GitLab settings variables override `.gitlab-ci.yml` ones, so reusing a name would leak production values into the integration-test job.

## 7. First deployment (one-off ordering)

The ECS service needs a task definition to exist before it can be created, and the pipeline creates the task definition. So the very first time:

1. Create the **ECS cluster** (Fargate) if it doesn't exist.
2. Merge to `main` (or re-run the `main` pipeline). `build-and-push` and `migrate` succeed: the image is in ECR, task definition `user-service:1` is registered and the migration has run. `deploy` fails because the service doesn't exist yet. That's expected.
3. **Create the ECS service** in the console:
   - Launch type Fargate, task definition `user-service` (latest revision), service name `user-service`, desired count 1 or more.
   - Networking: the private subnets and the `user-service` security group, no public IP.
   - Load balancing: the internal ALB, a new target group (type IP, port 8000) with health check path `/health`.
   - Deployment options: turn on the **deployment circuit breaker with rollback**. `ecs-deploy.sh` relies on it to detect bad releases.
4. Re-run the failed `deploy` job. From then on every merge to `main` deploys on its own.

### The worker (RabbitMQ consumer)

The same image also runs as a second ECS service, `user-service-worker` (`python -m app.worker`, [`worker-task-definition.json`](worker-task-definition.json)). It consumes `user-service.OrderConfirmed` and `user-service.EventCancelled` from the shared broker. Before its first deploy:

1. **Broker login:** rabbitmq-service's `tofu apply` creates the secret `rabibi/service/user-service`. user-service's `tofu apply` reads it, so run the broker's apply first.
2. **Broker network access:** add the `user-service` security group to `amqp_allowed_sg_ids` in rabbitmq-service's `terraform.tfvars`, then `tofu apply` there.
3. `tofu apply` here creates the worker service, and `tofu output -json gitlab_ci_variables` now includes `ECS_WORKER_SERVICE` and `RABBITMQ_SECRET_ARN`.

`migrate` registers both task definitions, and `deploy` rolls out the API, then the worker. The worker uses `desired_count` like the API, so scaling to 0 stops both.

## 8. Checking a deployment

- **GitLab:** the pipeline shows `migrate` and `deploy` green, and the **Operate → Environments → production** page lists the deployed commit.
- **ECS:** the service's primary deployment uses the new task definition revision, with tasks `RUNNING` and `HEALTHY`.
- **ALB:** the targets in the target group are healthy.
- **CloudWatch** `/ecs/user-service`: JSON log lines, including the migration task's `Running upgrade` output.
- **End to end:** the AWS smoke tests (Cognito token → `POST /api/v1/users/me` through API Gateway).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `build-and-push` fails at `AssumeRoleWithWebIdentity` / "Not authorized" | Trust policy `sub` doesn't match (project path or branch), or the OIDC provider's audience isn't `https://gitlab.com` |
| Variables are empty in a `main` job | Variable not protected-compatible (`main` not protected), or the environment scope isn't `production` |
| `migrate` fails with `ResourceInitializationError` / can't pull image or secret | Private subnets have no route to ECR / Secrets Manager (NAT or VPC endpoints), or the execution role lacks the secret permission |
| `migrate` exits 1 | Read the migration task's log stream (printed by the job): usually the RDS security group or wrong credentials |
| `deploy` says ECS rolled back | New tasks failed health checks. Check the tasks' stopped reason and logs. Common causes: Cognito pool ID or DB settings wrong, or the security group doesn't allow the ALB on port 8000 |
