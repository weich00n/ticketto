# Deploying checkout-service to AWS

Checkout runs as **two ECS services from one image** on Fargate: the API (`checkout-service`, port `3002`, ALB health path `/health`) and the worker (`checkout-service-worker`, command `python -m app.worker`, no load balancer). The API is HTTP-only; all background work (outbox relay, the deadline sweep, the confirm retry, the refund processor and the three queue consumers) lives in the worker.

This page keeps **two deployment guides**, for the identical checkout-service/ECS architecture. They differ only in *who signs in to AWS* and *what triggers a release*:

- **Guide A — the GitLab deploy guide (CI/CD pipeline + OIDC):** the team-standard, automated path. `.gitlab-ci.yml` builds, migrates and deploys every `main` commit; GitLab signs in to AWS with a short-lived OIDC token that assumes a role restricted to this project's `main` branch (no AWS keys in GitLab). Set it up once and `main` deploys on its own.
- **Guide B — the AWS deploy guide (manual, your own credentials):** you drive AWS yourself from your terminal with your own identity (IAM user or SSO) — build the image, then run the same two scripts by hand. No GitLab, no OIDC.

Both guides share the one-time **AWS** resources in **§1–§6** (create those once in the console). After that, the GitLab guide continues in **§7–§9** (GitLab OIDC + CI/CD setup) and the AWS guide continues in **§10–§13** (your credentials + the deploy loop). Follow whichever guide matches how you're shipping.

Replace `<ACCOUNT_ID>` with your AWS account ID; the region is `us-east-1` throughout. Steps marked *shared* may already exist from the other services: reuse them rather than creating a second one.

> **How it is actually deployed in the team account (`708677596839`).** §1–§7 and §9's console steps are done by [`terraform/`](terraform/README.md): the security group and its rules, ECR, the log group, the Stripe secret, the execution role, the `gitlab-checkout-service-deploy` role (§7) and both ECS services in the `TicketPlatform` cluster. Differences from the general guide below:
>
> - **Network:** the default VPC's **public** subnets with a public IP per task (no NAT, no VPC endpoints), like the other services. So the migration task needs `ECS_ASSIGN_PUBLIC_IP=ENABLED` (§8).
> - **API Gateway in front of the shared ALB:** the team's shared HTTP API `api` reaches the API tasks through the shared VPC link `ticketto-shared-services` and checkout's own listener (3002) on the shared internal ALB `ticketto-internal-api`, as for every other service ([`terraform/gateway.tf`](terraform/gateway.tf)). The ALB, listener, target group `ticketto-checkout-api` and the ALB-to-task rule belong to `frontend/deploy/gateway`: apply it first. `/api/v1/orders*` uses the Cognito JWT authorizer and the gateway sets `X-User-Sub`; `POST /webhooks/stripe` has no authorizer and is checked by its `Stripe-Signature`. Nothing else (`/health`, `/ready`) is routed. Read "ALB" in the guide below as this gateway.
> - **Who owns what:** the pipeline owns releases (image, task definition revisions, migration, rollout). Terraform owns the infrastructure and each service's desired count, and ignores which revision a service runs, so `terraform apply` never rolls a pipeline deploy back.
> - **CI/CD variables:** `terraform output gitlab_ci_variables` prints every §8 value for this account.

## The checkout deltas vs event-service

Checkout is the same shape as event-service, with these additions on top (everything below is identical to event-service except where noted):

- **Three** Secrets Manager secrets instead of two: `DB_SECRET_ARN`, `RABBITMQ_SECRET_ARN` and `STRIPE_SECRET_ARN` (§4).
- An extra value, `RESERVATION_BASE_URL`, on **both** task definitions (§8/§11) — the address checkout calls the reservation-service.
- The API container port is **3002** (event-service is 3001); the worker task has no port mapping.
- The database is `checkout_service` on **MySQL 8.4**; TLS stays **on** against RDS (§2).
- A **Stripe webhook** (`POST /webhooks/stripe`) called directly by Stripe over HTTPS, so the **ALB** (not the task) fronts it (§1).

---

## One-time AWS setup (both guides)

### 1. Network prerequisites

The ECS tasks run in **private subnets**, but still need outbound access to ECR (pull image), CloudWatch Logs, Secrets Manager, RabbitMQ and — for the API's PaymentIntent calls and the Stripe webhook — Stripe (a public HTTPS endpoint). Provide one of (*shared*):
- a **NAT gateway** for the private subnets (simplest), or
- **VPC endpoints**: `ecr.api`, `ecr.dkr`, `s3` (gateway), `logs`, `secretsmanager` — *but note that Stripe is a public endpoint and is not covered by VPC endpoints, so for full Stripe access the NAT gateway path is what actually reaches it.*

Security groups:

| Security group | Inbound rule |
|---|---|
| `checkout-service` (both services' tasks) | TCP **3002** from the internal ALB's security group, and from the Reservation group (checkout calls reservation's `/internal/v1`) |
| RDS | TCP 3306 from the `checkout-service` group (and each other service's group for its own DB) |
| RabbitMQ | AMQP (5671 for TLS, 5672 otherwise) from the `checkout-service` group |
| Stripe webhook | Stripe calls `POST /webhooks/stripe` **directly over HTTPS**, but the webhook is served by the **ALB**, not the task — the task's own security group does not need an inbound Stripe rule. Instead the ALB must route `/webhooks/stripe/*` to the `checkout-service` target group, and the ALB's listener/security group must accept Stripe's IPs on 443 |

The service trusts the `X-User-Sub` header the gateway sets, so it must accept traffic only from the ALB and other services. The Stripe webhook route has no gateway route and no `X-User-Sub`; its only authentication is the `Stripe-Signature` header.

### 2. RDS database and credentials

On the shared RDS **MySQL 8.4** instance:

```sql
CREATE DATABASE checkout_service CHARACTER SET utf8mb4;
CREATE USER 'checkout_service'@'%' IDENTIFIED BY '<strong password>';
GRANT ALL PRIVILEGES ON checkout_service.* TO 'checkout_service'@'%';
```

Store the credentials in **Secrets Manager** as a secret whose JSON has the keys `username` and `password`. The task definitions read exactly those two keys:

```json
{ "username": "checkout_service", "password": "<strong password>" }
```

Note the secret's ARN — you'll need it as `DB_SECRET_ARN` later.

**MySQL 8.4 / `caching_sha2_password` notes** (why the connection is wired the way it is): MySQL 8's default auth plugin is `caching_sha2_password`. Over a non-TLS connection the client must do an RSA full-auth exchange, which needs the `cryptography` package (already in the image). `app/config.py`'s `database_connect_args()` handles this: **against RDS it keeps TLS on** (`DB_SSL_CA` set → the driver verifies the server against the bundled RDS CA and never drops to plain); **for non-RDS / local it disables TLS** (`ssl_disabled=True`) so PyMySQL takes the deterministic RSA path instead of a preferred-TLS upgrade that caching_sha2 can reject with `1045`. The task definition sets `DB_SSL_CA=/etc/ssl/rds/global-bundle.pem`, so the deployed path always uses TLS to the RDS endpoint. `DB_PORT` is `3306`; `DB_NAME` is `checkout_service`.

### 3. RabbitMQ connection secret

Store the broker URL in **Secrets Manager** as a **plain-text** secret, the whole value being the URL. For Amazon MQ that looks like:

```text
amqps://checkout_service:<password>@b-xxxx.mq.us-east-1.amazonaws.com:5671/%2F
```

Note the secret's ARN — you'll need it as `RABBITMQ_SECRET_ARN` later. The worker publishes to the durable `ticketto.events` topic exchange (declared if missing) and consumes the three pre-created checkout quorum queues; the broker owns the full topology, so the client never declares queues.

### 4. Stripe secret

Store both Stripe keys in **one** **Secrets Manager** secret whose JSON has the keys `STRIPE_API_KEY` and `STRIPE_WEBHOOK_SECRET`. The task definitions read both keys from the same ARN:

```json
{ "STRIPE_API_KEY": "sk_test_...", "STRIPE_WEBHOOK_SECRET": "whsec_..." }
```

Note the secret's ARN — you'll need it as `STRIPE_SECRET_ARN` later. `STRIPE_API_KEY` is the test-mode or live key used for the PaymentIntent calls; `STRIPE_WEBHOOK_SECRET` verifies the inbound webhook signature.

### 5. ECR repository and log group

- **ECR → Create repository:** `checkout-service`, private, mutable tags (a deploy re-tags `latest`). Note its URI, e.g. `<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/checkout-service`. One repository serves **both** services; they run the same image.
- **CloudWatch Logs → Create log group:** `/ecs/checkout-service`. The API logs under the `checkout-service/` stream prefix, the worker under `worker/`, and the one-off migration task under `checkout-service/checkout-service/<task-id>`.

### 6. IAM execution role

**Task execution role** (`checkout-service-execution`): used by ECS to start the containers.
- Trusted entity: `ecs-tasks.amazonaws.com`.
- Attach the managed policy `AmazonECSTaskExecutionRolePolicy` (ECR pull, CloudWatch logs).
- Add an inline policy so it can read **all three** secrets:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": ["<DB secret ARN>", "<RabbitMQ secret ARN>", "<Stripe secret ARN>"]
    }]
  }
  ```

There is **no task role**. The application code calls no AWS APIs: it talks to MySQL, RabbitMQ and Stripe.

---

## Guide A — the GitLab deploy guide (CI/CD pipeline + OIDC)

The pipeline (`.gitlab-ci.yml`) runs, on every commit to the default branch:
1. **build-and-push** — builds the `runtime` image and pushes it to ECR as `:<commit SHA>` and `:latest`.
2. **migrate** (`scripts/ci/ecs-migrate.sh`) — fills in [`task-definition.json`](task-definition.json) and [`worker-task-definition.json`](worker-task-definition.json), registers a new revision of each, then runs `alembic upgrade head` once as a one-off task with the API revision; fails unless the task exits 0.
3. **deploy** (`scripts/ci/ecs-deploy.sh`) — points both ECS services at their new revisions and waits until both are stable; fails if ECS rolled either back.

AWS jobs run only when the `DEPLOY_ENABLED` variable is `true`, so `main` stays green (checks + image build) before AWS is wired up.

### 7. GitLab OIDC: let the pipeline sign in to AWS

**IAM → Identity providers → Add provider** (*shared*: once per AWS account):
- Type: OpenID Connect
- Provider URL: `https://gitlab.com`
- Audience: `https://gitlab.com`

In the team account the provider exists and Terraform creates the role below (`aws_iam_role.gitlab_deploy`); its ARN is `terraform output -raw gitlab_deploy_role_arn`.

**IAM → Roles → Create role** `gitlab-checkout-service-deploy`, with this **trust policy**. The `sub` condition is what restricts it to this project's `main` branch (the project path follows the `cs302-2026/g2-team5/backend/` convention the sibling services use):

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
        "gitlab.com:sub": "project_path:cs302-2026/g2-team5/backend/checkout-service:ref_type:branch:ref:main"
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
      "Resource": "arn:aws:ecr:us-east-1:<ACCOUNT_ID>:repository/checkout-service"
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
      "Resource": "arn:aws:iam::<ACCOUNT_ID>:role/checkout-service-execution",
      "Condition": { "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" } }
    }
  ]
}
```

`EcsDeploy` uses `"*"` because registering a task definition can't be limited by resource; tighten `RunTask`, `UpdateService` and `DescribeServices` to the cluster/service ARNs once they exist.

### 8. GitLab CI/CD variables

**GitLab → Settings → CI/CD → Variables** in the checkout-service project. For each variable: tick **Protect variable** (only `main` sees it) and set **Environment scope** to `production` (only the deploy jobs see it). None is a password (secrets stay in Secrets Manager), so masking is optional.

| Variable | Example |
|---|---|
| `AWS_REGION` | `us-east-1` |
| `AWS_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/gitlab-checkout-service-deploy` |
| `ECR_REPOSITORY_URI` | `<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/checkout-service` |
| `ECS_CLUSTER` | `TicketPlatform` |
| `ECS_SERVICE` | `checkout-service` |
| `ECS_WORKER_SERVICE` | `checkout-service-worker` |
| `ECS_SUBNETS` | `subnet-aaa,subnet-bbb` (comma-separated, no spaces) |
| `ECS_SECURITY_GROUP` | `sg-...` (the `checkout-service` group) |
| `ECS_ASSIGN_PUBLIC_IP` | `ENABLED` for public subnets without NAT (the team account); leave unset (`DISABLED`) for private subnets |
| `ECS_EXECUTION_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/checkout-service-execution` |
| `RDS_ENDPOINT` | `<instance>.<id>.us-east-1.rds.amazonaws.com` |
| `DB_SECRET_ARN` | `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:...` |
| `RABBITMQ_SECRET_ARN` | `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:...`. A plain-text secret as in §3; for a JSON secret, append the key, e.g. `…:secret:rabibi/service/checkout-service-XXXXXX:url::` (the team account's broker secret is JSON with key `url`) |
| `STRIPE_SECRET_ARN` | `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:...` |
| `RESERVATION_BASE_URL` | `http://reservation-service.ticketto.local:8081` (its Cloud Map name) |

In the team account, `terraform output gitlab_ci_variables` (in `deploy/terraform`) prints all of these with the real values.

Make `main` a protected branch (default), or protected variables won't reach it. Finally add **`DEPLOY_ENABLED`** = `true`: protected, but with environment scope **All (`*`)**, because the pipeline reads it to decide which jobs to create. Until it is set, `main` pipelines run the checks and build the image but skip `build-and-push`, `migrate` and `deploy`.

The names deliberately don't reuse the app's own variable names (`DB_HOST`, `RABBITMQ_URL`, `STRIPE_API_KEY`): GitLab settings variables override `.gitlab-ci.yml` ones, so reusing a name would leak production values into the integration-test job.

### 9. First deployment (one-off ordering)

In the team account this is already done: Terraform created the task definitions and both services, and the migration has run. The first merge to `main` with `DEPLOY_ENABLED=true` deploys directly, so skip the steps below.

The ECS services need task definitions to exist before they can be created, and the pipeline creates the task definitions. So the very first time:
1. Create the **ECS cluster** (Fargate) if it doesn't exist (*shared*).
2. Merge to `main` (or re-run the `main` pipeline). `build-and-push` and `migrate` succeed: the image is in ECR, task definitions `checkout-service:1` and `checkout-service-worker:1` are registered and the migration has run. `deploy` fails because the services don't exist yet — expected.
3. **Create the API service** in the console: Fargate, task definition `checkout-service` (latest revision), name `checkout-service`, desired count ≥1; the private subnets + `checkout-service` security group, no public IP; load balancing to the internal ALB with a new target group (type IP, port **3002**) health path `/health`; deployment circuit breaker with rollback on (the deploy step relies on it).
4. **Create the worker service**: task definition `checkout-service-worker`, name `checkout-service-worker`, desired count 1; same subnets and security group, no public IP, **no load balancer**; circuit breaker with rollback on.
5. Re-run the failed `deploy` job. From then on every merge to `main` deploys on its own.

**Checking a deployment:** GitLab shows `migrate` and `deploy` green and **Operate → Environments → production** lists the deployed commit; ECS shows both services' primary deployments on the new revisions (API tasks `RUNNING`/`HEALTHY`, worker tasks `RUNNING`); the ALB targets are healthy; CloudWatch `/ecs/checkout-service` shows the migration's `Running upgrade`, the worker's `outbox: delivered` and the consumers' `consuming`; and an end-to-end order → PaymentIntent → confirmed order issues a ticket (ticket-service consumed `OrderConfirmed`).

**Troubleshooting (automated):**

| Symptom | Likely cause |
|---|---|
| `build-and-push` / `migrate` fails at `AssumeRoleWithWebIdentity` / "Not authorized" | Trust policy `sub` doesn't match (project path or branch), or the OIDC provider's audience isn't `https://gitlab.com` |
| Variables empty in a `main` job | Variable not protected (`main` not protected) or environment scope isn't `production` |
| `migrate` fails `ResourceInitializationError` / can't pull image or secret | The task has no route to ECR / Secrets Manager: set `ECS_ASSIGN_PUBLIC_IP=ENABLED` for public subnets, or add NAT or VPC endpoints for private ones. Or the execution role can't read one of the **three** secrets (for the JSON broker secret, check the `:url::` ending) |
| `migrate` exits 1 | Read the migration task's log stream (`checkout-service/checkout-service/<task-id>`): usually the RDS security group or wrong credentials |
| `deploy` says `checkout-service` rolled back | New tasks failed health checks; check the task's stopped reason and logs (DB settings, or ALB not allowed on port **3002**) |
| Worker runs but no messages arrive | Worker log stream: `outbox relay cycle failed` / `Outbox delivery failed` — RabbitMQ URL, TLS port or security group. Undelivered messages stay in `outbox_messages` and are retried, so nothing is lost |
| Webhook `400 BAD_SIGNATURE` from Stripe | `STRIPE_WEBHOOK_SECRET` in Secrets Manager doesn't match the endpoint secret in the Stripe dashboard, or the ALB isn't forwarding the raw body |

---

## Guide B — the AWS deploy guide (manual, your own credentials)

No GitLab, no OIDC: you build the image and run the two scripts yourself. The same `ecs-migrate.sh` / `ecs-deploy.sh` are used — they just expect the §11 values as environment variables instead of CI/CD variables. Every release is four moves: **build + push** → **migrate** → **deploy** → **verify** (§13).

### 10. Your AWS credentials

Your own AWS identity signs in. Use an IAM user or an SSO-assumed role that can do: `ecr:GetAuthorizationToken`; the ECR push actions on the `checkout-service` repository; `ecs:RegisterTaskDefinition`, `ecs:DescribeTaskDefinition`, `ecs:RunTask`, `ecs:DescribeTasks`, `ecs:UpdateService`, `ecs:DescribeServices`; and `iam:PassRole` on the **execution role** (condition `iam:PassedToService: ecs-tasks.amazonaws.com`). The policy (same as §7's, expressed for your identity, no OIDC):

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
      "Resource": "arn:aws:ecr:us-east-1:<ACCOUNT_ID>:repository/checkout-service"
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
      "Resource": "arn:aws:iam::<ACCOUNT_ID>:role/checkout-service-execution",
      "Condition": { "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" } }
    }
  ]
}
```

`EcsDeploy` uses `"*"` because registering a task definition can't be limited by resource; tighten `RunTask`, `UpdateService` and `DescribeServices` to the cluster/service ARNs once they exist. The one-time resources (cluster, services, ALB target group, RDS, secrets, execution role) are easiest to create in the **console**; the policy above covers only the repeated deploy loop.

Configure the CLI once in the directory you deploy from:

```sh
aws configure          # access key id / secret / region us-east-1   — or:
aws sso login          # if you use IAM Identity Center
aws sts get-caller-identity   # sanity check
```

Never commit the keys; they stay in `~/.aws` (or the SSO session).

### 11. The values a deploy needs

Everything the scripts read is an environment variable. In CI these are protected CI/CD variables (§8); manually, export them yourself — keep them in a file you **don't commit** (the repo git-ignores `deploy.env`) or re-export per session.

| Variable | Value |
|---|---|
| `AWS_REGION` | `us-east-1` |
| `ECR_REPOSITORY_URI` | `<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/checkout-service` |
| `IMAGE` | `$ECR_REPOSITORY_URI:<tag>` — the exact image you just pushed (§12) |
| `ECS_CLUSTER` | `ticketto` |
| `ECS_SERVICE` | `checkout-service` |
| `ECS_WORKER_SERVICE` | `checkout-service-worker` |
| `ECS_SUBNETS` | `subnet-aaa,subnet-bbb` (private subnets, comma-separated, no spaces) |
| `ECS_SECURITY_GROUP` | `sg-...` (the `checkout-service` group) |
| `ECS_EXECUTION_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/checkout-service-execution` |
| `RDS_ENDPOINT` | `<instance>.<id>.us-east-1.rds.amazonaws.com` |
| `DB_SECRET_ARN` | `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:...` (§2) |
| `RABBITMQ_SECRET_ARN` | `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:...` (§3) |
| `STRIPE_SECRET_ARN` | `arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:...` (§4) |
| `RESERVATION_BASE_URL` | `http://reservation-service:8081` (service-to-service name) |

`RESERVATION_BASE_URL` is the one value that isn't a secret — the address checkout uses to call the reservation-service over the VPC. Until the Java reservation-service ships a runnable image, this is aspirational: the deployed VPC won't have that endpoint yet.

### 12. First deployment

One-time in the console (if not already done): the **ECS cluster** (Fargate, *shared*) and the §1–§6 resources (RDS DB + user + the two secrets, the Stripe secret, ECR repo + log group, execution role).

Then, from a checkout of this repository:

1. **Build** the `runtime` image (the same image the worker uses, with a `python -m app.worker` command override):

   ```sh
   export IMAGE="$ECR_REPOSITORY_URI:v0.1.0"   # any tag; use the commit SHA in a git repo
   docker build --target runtime -t "$IMAGE" .
   ```

2. **Push** to ECR:

   ```sh
   aws ecr get-login-password --region us-east-1 \
     | docker login --username AWS --password-stdin "${ECR_REPOSITORY_URI%%/*}"
   docker push "$IMAGE"
   ```

3. **Export all the §11 values** (the scripts abort with `:?` on any missing one) and run the migration step:

   ```sh
   bash scripts/ci/ecs-migrate.sh
   ```

   Registers task definitions `checkout-service:1` and `checkout-service-worker:1`, runs `alembic upgrade head` as a one-off Fargate task, and exits non-zero unless the migration exits 0. On success it writes `deploy.env` holding the two new task-definition ARNs.

4. **Create the API service** in the console (first deploy has no service to update): Fargate, task definition `checkout-service` (latest revision), name `checkout-service`, desired count ≥1; private subnets + `checkout-service` security group, no public IP; ALB with a new target group (type IP, port **3002**) health path `/health`; deployment circuit breaker with rollback on.

5. **Create the worker service**: task definition `checkout-service-worker`, name `checkout-service-worker`, desired count 1; same subnets + security group, no public IP, **no load balancer**; circuit breaker with rollback on.

6. **Roll both services** to the new revisions (source the ARNs the migrate step wrote):

   ```sh
   source deploy.env
   bash scripts/ci/ecs-deploy.sh
   ```

   Updates both services, waits until both are stable, and exits non-zero if either is left on the *old* revision (ECS rolled back) or the rollout isn't `COMPLETED`.

### 13. Subsequent deploys & checking

Every later release is the same loop from your terminal (no console work):

```sh
export IMAGE="$ECR_REPOSITORY_URI:<new-tag>"
docker build --target runtime -t "$IMAGE" .
docker push "$IMAGE"
bash scripts/ci/ecs-migrate.sh          # new revisions + re-runs `alembic upgrade head`
source deploy.env
bash scripts/ci/ecs-deploy.sh           # rolls both services, fails on a rollback
```

After a release you've verified in production, re-tag `latest` so the next build can use it as a layer cache:

```sh
docker tag "$IMAGE" "$ECR_REPOSITORY_URI:latest" && docker push "$ECR_REPOSITORY_URI:latest"
```

**Checking a deployment:** both services' primary deployments on the new revisions; the API's tasks `RUNNING` and `HEALTHY`, the worker's `RUNNING` (no HTTP server, so no health check — its liveness is its log stream); the ALB targets healthy; CloudWatch `/ecs/checkout-service` shows the migration's `Running upgrade`, the worker's `outbox: delivered` and the consumers' `consuming`; end to end, an order → PaymentIntent → confirmed order issues a ticket (ticket-service consumed `OrderConfirmed`).

**Troubleshooting (manual):**

| Symptom | Likely cause |
|---|---|
| `docker login` / `docker push` auth error | Credentials lack `ecr:GetAuthorizationToken` or the ECR push actions (§10), or the repo name in `ECR_REPOSITORY_URI` is wrong |
| `ecs-migrate.sh` aborts with `<VAR>` errors | A §11 value is missing from the environment — the script names exactly which |
| `migrate` fails `ResourceInitializationError` / can't pull image or secret | Private subnets have no route to ECR / Secrets Manager (NAT or VPC endpoints), or the execution role can't read one of the **three** secrets |
| `migrate` exits 1 | Read the migration task's log stream (printed by the script): usually the RDS security group or wrong credentials |
| `deploy` says a service rolled back | New tasks failed health checks; check the task's stopped reason and logs (DB settings, or ALB not allowed on port **3002**) |
| Worker runs but no messages arrive | Worker log stream: `outbox relay cycle failed` / `Outbox delivery failed` — RabbitMQ URL, TLS port or security group. Undelivered messages stay in `outbox_messages` and are retried, so nothing is lost |
| Webhook `400 BAD_SIGNATURE` from Stripe | `STRIPE_WEBHOOK_SECRET` in Secrets Manager doesn't match the Stripe dashboard endpoint secret, or the ALB isn't forwarding the raw body |
| Webhook `404` from Stripe | The ALB has no route for `/webhooks/stripe/*`, or the target group port isn't **3002** |
