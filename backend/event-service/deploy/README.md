# Deploying event-service to AWS

The GitLab pipeline (`.gitlab-ci.yml`) builds every commit on `main` and pushes the image to ECR; deploying it to ECS on Fargate is a manual button: press **`migrate`** on the `main` pipeline, and `deploy` follows once it succeeds. Event Service runs as **two ECS services from one image**: the API (`event-service`) and the outbox worker (`event-service-worker`, command `python -m app.worker`).

1. **build-and-push** builds the `runtime` image and pushes it to ECR as `:<commit SHA>` and `:latest`.
2. **migrate** ([`scripts/ci/ecs-migrate.sh`](../scripts/ci/ecs-migrate.sh)):
   - fills in [`task-definition.json`](task-definition.json) and [`worker-task-definition.json`](worker-task-definition.json) and registers a new revision of each;
   - runs `alembic upgrade head` once as a one-off task with the API revision;
   - fails unless the task exits with code 0.
3. **deploy** ([`scripts/ci/ecs-deploy.sh`](../scripts/ci/ecs-deploy.sh)):
   - points both ECS services at their new revisions and waits until both are stable;
   - fails if ECS rolled either back to the previous version.

GitLab signs in to AWS with **OIDC**: each job gets a short-lived token, and AWS lets it assume an IAM role only for this project's `main` branch. No AWS keys are stored in GitLab.

This page is the one-time setup. Everything is done in the AWS console and GitLab settings. Replace `<ACCOUNT_ID>` with your AWS account ID; the region is `us-east-1` throughout. Steps marked *shared* may already exist from user-service: reuse them rather than creating a second one.

**The AWS resources are also defined as code** in [`terraform/`](terraform/) (OpenTofu): the ECR repository, log group, the two IAM roles and the two ECS services. Use it instead of clicking through steps 4, 5, 6 and 8. Resources that were first created by hand are adopted, not rebuilt: see [Managing the resources with OpenTofu](#managing-the-resources-with-opentofu). The database, its login secret, the security groups and the broker are shared and stay outside it.

---

## 1. Network prerequisites

The ECS tasks need outbound access to ECR (pull image), CloudWatch Logs, Secrets Manager and RabbitMQ. Provide one of (*shared*):
- **public subnets of the default VPC with a public IP on each task**, which is how reservation-service runs. Set `ECS_ASSIGN_PUBLIC_IP` to `ENABLED` (step 7) so the migration task gets one too. This is also the VPC the RabbitMQ broker is in;
- **private subnets** with a **NAT gateway**, or
- **private subnets** with **VPC endpoints**: `ecr.api`, `ecr.dkr`, `s3` (gateway), `logs`, `secretsmanager`.

Security groups:

| Security group | Inbound rule |
|---|---|
| `event-service` (both services' tasks) | TCP 3001 from Reservation's and Checkout's groups for `/internal/v1`, and from the shared internal ALB's group (`ticketto-internal-api-alb`, added by the gateway stack). Never open it to `0.0.0.0/0`: the service trusts the `X-User-*` headers |
| RDS | TCP 3306 from the `event-service` group (and each other service's group for its own DB) |
| RabbitMQ | AMQP 5672 from the `event-service` group. The broker owner adds it (see step 3) |

The service trusts the `X-User-*` headers API Gateway sets, so it must accept traffic only from API Gateway's path (the ALB) and other services. Give `/internal/v1/*` no API Gateway route.

## 2. RDS database and credentials

On the shared RDS MySQL instance:

```sql
CREATE DATABASE db_event CHARACTER SET utf8mb4;
CREATE USER 'event_service'@'%' IDENTIFIED BY '<strong password>';
GRANT ALL PRIVILEGES ON db_event.* TO 'event_service'@'%';
```

Store the credentials in **Secrets Manager** as a secret whose JSON has the keys `username` and `password`. The task definitions read exactly those two keys:

```json
{ "username": "event_service", "password": "<strong password>" }
```

Note the secret's ARN. Use a password of letters and digits only (for example `openssl rand -hex 16`), and don't use the instance's `rds!` master secret for the service: RDS rotates it, and running tasks only read secrets when they start.

The RDS instance must be running when the `migrate` job runs. If the user already exists with a different password, reset it with `ALTER USER 'event_service'@'%' IDENTIFIED BY '<password in the secret>';`, then retry `migrate`. A `1045 Access denied` in the migration log means the secret and MySQL disagree.

## 3. RabbitMQ connection secret

The shared broker runs on its own EC2 instance from rabbitmq-service ([its `deploy/README.md`](https://gitlab.com/cs302-2026/g2-team5/backend/rabbitmq-service/-/blob/main/deploy/README.md)). Ask its owner for two things:

- **The secret's ARN.** Each service has its own broker user and secret, named `rabibi/service/event-service` for this service. Its `url` key holds the full URL, plain AMQP on port 5672, which the task definitions read as `RABBITMQ_URL`. Use the ARN for the `RABBITMQ_SECRET_ARN` variable.
- **Access for this service.** Only listed security groups can reach port 5672, so they add the `event-service` group to `amqp_allowed_sg_ids` and run `tofu apply`.

The broker is in the account's **default VPC** (`172.31.0.0/16`). The ECS tasks must run in that VPC, or in a VPC peered with it; otherwise the worker can't connect and messages wait in `outbox_messages`.

## 4. ECR repository and log group

- **ECR → Create repository:** `event-service`, private, mutable tags (the pipeline re-tags `latest`). Note its URI, e.g. `<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/event-service`.
- **CloudWatch Logs → Create log group:** `/ecs/event-service`. The API logs under the `event-service/` stream prefix, the worker under `worker/`.

## 5. IAM role used by the tasks

**Task execution role** (`event-service-execution`): used by ECS to start the containers.
- Trusted entity: `ecs-tasks.amazonaws.com`.
- Attach the managed policy `AmazonECSTaskExecutionRolePolicy` (ECR pull, CloudWatch logs).
- Add an inline policy so it can read both secrets:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": ["<DB secret ARN>", "<RabbitMQ secret ARN>"]
    }]
  }
  ```

There is **no task role**. Unlike user-service, the application code calls no AWS APIs: it only talks to MySQL and RabbitMQ.

## 6. GitLab OIDC: let the pipeline sign in to AWS

**IAM → Identity providers → Add provider** (*shared*: once per AWS account):
- Type: OpenID Connect
- Provider URL: `https://gitlab.com`
- Audience: `https://gitlab.com`

**IAM → Roles → Create role** `gitlab-event-service-deploy`, with this **trust policy**. The `sub` condition is what restricts it to this project's `main` branch:

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
        "gitlab.com:sub": "project_path:cs302-2026/g2-team5/backend/event-service:ref_type:branch:ref:main"
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
      "Resource": "arn:aws:ecr:us-east-1:<ACCOUNT_ID>:repository/event-service"
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
      "Sid": "PassExecutionRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::<ACCOUNT_ID>:role/event-service-execution",
      "Condition": { "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" } }
    }
  ]
}
```

`EcsDeploy` uses `"*"` because registering a task definition can't be limited by resource. You can tighten `RunTask`, `UpdateService` and `DescribeServices` to the cluster and service ARNs once they exist.

## 7. GitLab CI/CD variables

**GitLab → Settings → CI/CD → Variables** in the event-service project. For each variable:
- tick **Protect variable**, so only `main` sees it;
- set **Environment scope** to `production`, so only the deploy jobs see it.

None of these is a password (the secrets stay in Secrets Manager), so masking is optional.

| Variable | Example |
|---|---|
| `AWS_REGION` | `us-east-1` |
| `AWS_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/gitlab-event-service-deploy` |
| `ECR_REPOSITORY_URI` | `<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/event-service` |
| `ECS_CLUSTER` | `TicketPlatform` |
| `ECS_SERVICE` | `event-service` (must match the ECS service name exactly) |
| `ECS_WORKER_SERVICE` | `event-service-worker` (same) |
| `ECS_SUBNETS` | `subnet-aaa,subnet-bbb` (comma-separated, no spaces) |
| `ECS_SECURITY_GROUP` | `sg-...` (the `event-service` group) |
| `ECS_ASSIGN_PUBLIC_IP` | `ENABLED` for public subnets; leave unset (`DISABLED`) for private subnets |
| `ECS_EXECUTION_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/event-service-execution` |
| `RDS_ENDPOINT` | `<instance>.<id>.us-east-1.rds.amazonaws.com` |
| `DB_SECRET_ARN` | `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:...` |
| `RABBITMQ_SECRET_ARN` | `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:...` |

Make `main` a protected branch (it is by default), or protected variables won't reach it.

Finally, add **`DEPLOY_ENABLED`** = `true`: protected, but with environment scope **All (`*`)**, because the pipeline reads it when deciding which jobs to create. Until it is set, `main` pipelines run the checks and build the image but skip `build-and-push`, `migrate` and `deploy`, so `main` stays green before AWS is ready.

The names deliberately don't reuse the app's own variable names (`DB_HOST`, `RABBITMQ_URL`). GitLab settings variables override `.gitlab-ci.yml` ones, so reusing a name would leak production values into the integration-test job.

## 8. First deployment (one-off ordering)

The ECS services need task definitions to exist before they can be created, and the pipeline creates the task definitions. So the very first time:

1. Use the existing **ECS cluster** (`TicketPlatform`), or create a Fargate cluster if there is none (*shared*).
2. Merge to `main` (or run a new `main` pipeline). `build-and-push` and `migrate` succeed: the image is in ECR, task definitions `event-service` and `event-service-worker` are registered and the migration has run. `deploy` fails with `ServiceNotFoundException` because the services don't exist yet. That's expected.
3. **Create the API service** in the console:
   - Compute options **Launch type**, **FARGATE** (the cluster has no default capacity provider). Task definition `event-service` (latest revision), desired count 1 or more.
   - Service name `event-service`. The console suggests a name like `event-service-service-ab12cd`: change it, because the pipeline looks the service up by name.
   - Networking: the subnets from `ECS_SUBNETS` and the `event-service` security group. Public IP on for public subnets, off for private ones.
   - Load balancing: the shared internal ALB `ticketto-internal-api`, existing target group `ticketto-event-api` (created by `frontend/deploy/gateway`), container port 3001.
   - Deployment options: turn on the **deployment circuit breaker with rollback**. `ecs-deploy.sh` relies on it to detect bad releases.
4. **Create the worker service**:
   - Task definition `event-service-worker`, service name `event-service-worker`, desired count 1 (never more: two workers would send each outbox message twice).
   - Same subnets and security group, **no load balancer**.
   - Deployment circuit breaker with rollback on, as above.
5. Run a new `main` pipeline and press **`migrate`** (or retry the `deploy` job if the variables haven't changed). From then on every merge to `main` builds and pushes the image, and you deploy it with the `migrate` button.

## 9. Checking a deployment

- **GitLab:** the pipeline shows `migrate` and `deploy` green, and the **Operate → Environments → production** page lists the deployed commit.
- **ECS:** both services' primary deployments use the new revisions. The API's tasks are `RUNNING` and `HEALTHY`; the worker's are `RUNNING`.
- **ALB:** the targets in the API's target group are healthy.
- **CloudWatch** `/ecs/event-service`: JSON log lines, including the migration task's `Running upgrade` output and the worker's `Outbox messages delivered`.
- **End to end:** publish an event through API Gateway, then check that Reservation received `EventPublished`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `build-and-push` fails at `AssumeRoleWithWebIdentity` / "Not authorized" | Trust policy `sub` doesn't match (project path or branch), or the OIDC provider's audience isn't `https://gitlab.com` |
| Variables are empty in a `main` job | Variable not protected-compatible (`main` not protected), or the environment scope isn't `production` |
| `migrate` fails with `ResourceInitializationError` / can't pull image or secret | The task has no route to ECR / Secrets Manager (set `ECS_ASSIGN_PUBLIC_IP=ENABLED` for public subnets, or add NAT or VPC endpoints for private ones), or the execution role can't read one of the two secrets |
| `migrate` exits 1 | Read the migration task's log stream (printed by the job): usually the RDS security group or wrong credentials |
| `migrate` log shows `Can't connect to MySQL server` | The RDS instance is stopped, or its security group has no inbound 3306 rule for the `event-service` group |
| `migrate` log shows `1045 Access denied for user 'event_service'` | The password in the DB secret doesn't match MySQL: run `ALTER USER` (step 2) and retry |
| `deploy` fails with `ServiceNotFoundException` | The ECS service doesn't exist, has a different name (check the console's suggested name), or is in another cluster. Fix `ECS_SERVICE`, `ECS_WORKER_SERVICE` or `ECS_CLUSTER`, then run a new pipeline: variables are read when a pipeline is created |
| `deploy` says `event-service` rolled back | New tasks failed health checks. Check the tasks' stopped reason and logs. Common causes: DB settings wrong, or the security group doesn't allow the ALB on port 3001 |
| Worker runs but no messages arrive | Check the worker's log stream for `Outbox delivery failed`: usually the broker's security group not listing the `event-service` group yet, or the tasks being in a VPC that isn't peered with the broker's. Undelivered messages stay in `outbox_messages` and are retried, so nothing is lost |

## Managing the resources with OpenTofu

[`terraform/`](terraform/) defines the ECR repository, the log group, the roles `event-service-execution` and `gitlab-event-service-deploy`, and the services `event-service` and `event-service-worker`. The pipeline still owns the image and each task definition revision; OpenTofu ignores later revisions and the task count, so applying never rolls the app back.

It looks up the VPC and subnets, cluster, existing dedicated `event-service` security group, `gitlab.com` identity provider, and secrets `event-service/db` and `rabibi/service/event-service`. Both Event tasks use the dedicated group instead of the broadly accessible default group. RDS already permits it; this stack adds its AMQP ingress rule to the broker group without changing other services' rules.

`terraform/gateway.tf` adds Event's Cloud Map registration (A + SRV), two public GET routes and six JWT-protected organiser routes to the existing HTTP API. The routes go through the team's shared VPC link (`ticketto-shared-services`) to Event's own listener (3001) on the shared internal ALB, like every other service; the ALB, listener, target group `ticketto-event-api` and the ALB-to-task rule belong to `frontend/deploy/gateway`, so apply that stack first. The Cloud Map name `event-service.ticketto.local:3001` is only for other services (Ticket, Notification). Organiser identity is mapped from verified `sub`, `username` and custom `groups` claims; public reads strip caller identity headers. Internal APIs and health endpoints are not published. The shared API, authorizer, default stage and other owners' routes are not managed here.

**Moving from Event's own VPC link to the shared ALB** (done once). Apply the gateway stack first, then attach the service to the target group before the routes switch over, so they never point at an empty target group:

```sh
tofu apply -target=aws_ecs_service.api   # new deployment registers the task in ticketto-event-api
aws elbv2 describe-target-health --target-group-arn <ticketto-event-api ARN>   # wait for healthy
tofu apply                               # routes move to the ALB; old VPC link and its group are deleted
```

The frontend may stay in demo mode while these routes are deployed. The shared Queue/Reservation/Ticket gateway stack and teammate handoff are in `frontend/deploy/gateway/README.md` in the sibling frontend repository.

**First time, with resources already created by hand.** [`terraform/import.tf`](terraform/import.tf) adopts them:

```sh
cd deploy/terraform
cp backend.hcl.example backend.hcl            # put in the state bucket name
export AWS_PROFILE=<your profile>             # credentials for the team's account
tofu init -backend-config=backend.hcl
tofu plan    # 9 to import, 1 to add, 6 to change, 0 to destroy
tofu apply
```

Read the plan before applying: it should destroy and replace nothing. The changes are tags, image scanning on the repository, and a cleanup rule that keeps the 20 newest images. Afterwards, delete `import.tf`.

**Changing the infrastructure later.** Edit the files in `terraform/`, then `tofu plan` and `tofu apply`. Don't change these resources in the console: the next apply would undo it.

The state lives in the team's state bucket (`rabibi-tfstate-<ACCOUNT_ID>`, key `event-service/terraform.tfstate`). It holds no passwords: the login secrets are created by hand and only looked up.
