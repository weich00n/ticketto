# Queue Service – Deploying to AWS

How the Queue service runs on AWS, what to set up once in AWS and GitLab, and what the pipeline does on every release. The background is in Phase 10 of `docs/implementation-plan.md`; the schema is in `docs/DB schema.md`.

Set everything up once for **staging**, verify it (Part E), then repeat for **prod**. `<env>` below is `staging` or `prod`. The region is `us-east-1` (where the shared RDS instance is).

## What gets deployed

**One image, three ECS services, one migration task.** The GitLab pipeline builds the image once (`release-image`), and every ECS task runs that same image with a different command.

| Name | ECS kind | Command | Copies |
| --- | --- | --- | --- |
| `queue-api` | **service**, behind a load balancer | *(image default)* uvicorn on port 8000 | 2+, can autoscale |
| `queue-admitter` | **service**, no load balancer | `python -m workers.admitter` | **exactly 1, always** |
| `queue-consumer` | **service**, no load balancer | `python -m workers.consumer` | 1 (2 is also safe) |
| `queue-migrate` | **one-off task**, run by the pipeline before each rollout | `alembic upgrade head` | runs once, exits |

Outside ECS:

| Dependency | Where | Status |
| --- | --- | --- |
| Redis | our own EC2 instance, Docker, from `ops/redis/user-data.sh` | set up |
| MySQL | database `queue_service` on the shared RDS instance `db-ticketing` | database and users `queue_app` / `queue_migrate` created |
| RabbitMQ | the central broker; we only consume | vhost and user to get from the broker team |

```
                         GitLab pipeline
  static_analysis → test → release-image ──push──► GitLab container registry
                                  │                        │ (ECS pulls with a deploy token)
                                  └── deploy ──────────────┼───────────────────────────────┐
                                        1. run queue-migrate ──► RDS: queue_service          │
                                        2. update queue-consumer, queue-admitter, queue-api ◄┘

  buyers ──► ALB ──► queue-api ─────┐
                      queue-admitter ┼──► Redis (EC2)        ──► RDS (queue_service)
  RabbitMQ ──► queue-consumer ──────┘
```

### Constraints of the current code (read before configuring)

The task definitions below are written for the code **as it is today**, so no code change is needed to deploy. Three things follow from that:

1. **Every process needs all of `MYSQL_URL`, `REDIS_URL`, `RABBITMQ_URL`, `JWT_PRIVATE_KEY_PATH` and `JWT_KID`**, because `Settings` requires them, even where a process doesn't use them (e.g. the migrate task). They come from a `.env` file in S3 (A2). Only the API actually reads the JWT key.
2. **The JWT key must be a file.** ECS passes secrets as env vars, so the API's command writes the `JWT_PRIVATE_KEY` secret to `/tmp/jwt_private.pem` and then starts uvicorn.
3. **On the shared broker the consumer declares nothing.** With `RABBITMQ_DECLARE_TOPOLOGY=false` it only checks that its four queues exist and then subscribes to them. It never declares the other teams' exchanges, nor its own queues, DLQs or bindings, so the broker team creates those (A10). If a queue is missing, the consumer logs `rabbitmq queue missing or not accessible` and keeps retrying. Locally the default (`true`) still declares the whole topology, so compose and the tests are unchanged.

MySQL runs **without TLS** for now: the RDS users were created without `REQUIRE SSL`. Turning TLS on needs code change C.

---

## Part A – AWS setup (once per environment)

Use the AWS console or the CLI. Names below are suggestions; keep whatever you already created.

### A1. Network and security groups

All ECS tasks run in the **same VPC as RDS and the Redis instance**.

| Security group | Inbound rules | Attached to |
| --- | --- | --- |
| `queue-lb-<env>-sg` | TCP 80/443 from where buyers come from (the gateway, or `0.0.0.0/0` for a public demo) | the ALB |
| `queue-app-<env>-sg` | TCP 8000 from `queue-lb-<env>-sg` | all 4 Queue task definitions |
| `queue-redis-<env>-sg` | TCP 6379 from `queue-app-<env>-sg`; TCP 5540 from My IP (RedisInsight) | the Redis EC2 instance |
| RDS's security group *(not ours; ask the owners if needed)* | **add** TCP 3306 from `queue-app-<env>-sg` | `db-ticketing` |

Ask the broker team to allow `queue-app-<env>-sg` (or the tasks' public/NAT IPs) on the AMQP port.

**Subnets:** the tasks must be able to reach the internet, to pull the image from `registry.gitlab.com` and to reach Secrets Manager and CloudWatch Logs. Either:
- **private subnets with a NAT gateway**, `assignPublicIp=DISABLED` (recommended), or
- **public subnets**, `assignPublicIp=ENABLED` (simpler; the security groups still block inbound traffic).

Note the subnet IDs and the `queue-app-<env>-sg` ID; the pipeline needs them (Part B).

### A2. Configuration: `.env` files in S3, the JWT key in Secrets Manager

**Connection URLs and plain settings** go in `.env` files in S3, which ECS loads into the containers (`environmentFiles`). Two files per environment, because the migrate task logs in as a different MySQL user:

`s3://<bucket>/queue-service/<env>/app.env`: used by `queue-api`, `queue-admitter`, `queue-consumer`

```
MYSQL_URL=mysql+asyncmy://queue_app:<pw>@db-ticketing.ckt0mcwoq5s8.us-east-1.rds.amazonaws.com:3306/queue_service
REDIS_URL=redis://:<redis-password>@<redis-private-ip>:6379/0
RABBITMQ_URL=amqps://<user>:<pw>@<broker-host>:5671/<vhost>
RABBITMQ_DECLARE_TOPOLOGY=false
JWT_KID=queue-staging-2026-10
JWT_PRIVATE_KEY_PATH=/tmp/jwt_private.pem
ADMITTER_TICK_SECONDS=1
LOG_FORMAT=json
LOG_LEVEL=INFO
```

`s3://<bucket>/queue-service/<env>/migrate.env`: used by `queue-migrate`

```
MYSQL_URL=mysql+asyncmy://queue_migrate:<pw>@db-ticketing.ckt0mcwoq5s8.us-east-1.rds.amazonaws.com:3306/queue_service
REDIS_URL=redis://unused:6379/0
RABBITMQ_URL=amqp://unused:5672/%2F
JWT_KID=unused
JWT_PRIVATE_KEY_PATH=/tmp/unused.pem
LOG_FORMAT=json
```

(The migrate task never connects to Redis or RabbitMQ; those values only satisfy `Settings`.)

Format rules ECS applies to these files:
- one `NAME=value` per line; lines starting with `#` are comments
- **no quotes and no `export`**: quotes become part of the value
- one line per variable, so a multi-line value such as a PEM key can't go in here
- URL-encode special characters in passwords (`@` → `%40`, `/` → `%2F`, `:` → `%3A`), and the vhost (`/` → `%2F`)
- `JWT_PRIVATE_KEY_PATH` points at the file the API's command writes; the workers never read it
- a changed file only reaches **new** tasks: after editing, redeploy (or ECS → service → Update → Force new deployment)

**The bucket holds database, Redis and RabbitMQ passwords**, so set it up as a secret store:
- Block all public access (on by default for new buckets), encryption on (SSE-S3 default, or SSE-KMS)
- **Versioning on**, so a bad edit can be rolled back
- a bucket policy (or just IAM) so only the ECS task execution role and the people who edit config can read `queue-service/*`

**The JWT signing key: keep it in Secrets Manager**, not in the S3 file. Recommendation and reasons:
- It's the most sensitive value in the service: whoever has it can mint valid admission tokens, i.e. let anyone skip the queue. Secrets Manager gives it its own IAM permission, audit trail (CloudTrail logs every read) and versioning, separate from the other config.
- It's a multi-line PEM, which an ECS `.env` file can't hold. (Base64-encoding it into one line would work, but puts it in the same file as everything else, readable by anyone who can read the config.)
- ECS injects it into the API container only, as `JWT_PRIVATE_KEY`; the admitter, consumer and migrate tasks never see it.

Create it once per environment, on your machine:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt_private.pem
aws secretsmanager create-secret --region us-east-1 \
  --name queue-service/staging/jwt-private-key --secret-string file://jwt_private.pem
rm jwt_private.pem
```

Use a **different key for staging and prod**, and put the matching `JWT_KID` in that environment's `app.env`. (Code change B in the implementation plan would let the app read the PEM from `JWT_PRIVATE_KEY` directly and drop the write-to-`/tmp` step from the API command.)

**One more Secrets Manager secret**, `queue-service/gitlab-registry`, JSON `{"username": "<deploy token username>", "password": "<deploy token>"}` (from B1), so ECS can pull the image. ECS's private-registry authentication only accepts a Secrets Manager secret.

GitLab never sees any of these values; it only tells ECS which image to run.

### A3. IAM roles

1. **Task execution role** `queue-service-exec-<env>` (ECS uses it to pull the image, read secrets and write logs):
   - trusted entity: Elastic Container Service → **Elastic Container Service Task**
   - managed policy `AmazonECSTaskExecutionRolePolicy`
   - inline policy (the `.env` files, the JWT key, the registry token):

     ```json
     {
       "Version": "2012-10-17",
       "Statement": [
         {
           "Effect": "Allow",
           "Action": "s3:GetObject",
           "Resource": "arn:aws:s3:::<bucket>/queue-service/<env>/*"
         },
         {
           "Effect": "Allow",
           "Action": "s3:GetBucketLocation",
           "Resource": "arn:aws:s3:::<bucket>"
         },
         {
           "Effect": "Allow",
           "Action": "secretsmanager:GetSecretValue",
           "Resource": [
             "arn:aws:secretsmanager:us-east-1:<account>:secret:queue-service/<env>/jwt-private-key-*",
             "arn:aws:secretsmanager:us-east-1:<account>:secret:queue-service/gitlab-registry-*"
           ]
         }
       ]
     }
     ```

     Add `kms:Decrypt` on the key if the bucket or the secrets use a customer-managed KMS key.

2. **Task role** `queue-service-task-<env>` (what the running app may do): the app calls no AWS APIs. Give it only this, so you can open a shell with ECS Exec (used for DLQ tools):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel",
                  "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"],
       "Resource": "*"
     }]
   }
   ```

3. **Deploy role for GitLab** `queue-service-gitlab-deploy` (what the pipeline may do). GitLab logs in with **OIDC**, so no AWS keys are stored in GitLab:
   1. IAM → Identity providers → Add provider → **OpenID Connect**, provider URL `https://gitlab.com`, audience `sts.amazonaws.com`.
   2. Create the role with this **trust policy** (only the `main` branch of this project can assume it):

      ```json
      {
        "Version": "2012-10-17",
        "Statement": [{
          "Effect": "Allow",
          "Principal": {"Federated": "arn:aws:iam::<account>:oidc-provider/gitlab.com"},
          "Action": "sts:AssumeRoleWithWebIdentity",
          "Condition": {
            "StringEquals": {"gitlab.com:aud": "sts.amazonaws.com"},
            "StringLike": {"gitlab.com:sub": "project_path:cs302-2026/g2-team5/backend/queue-service:ref_type:branch:ref:main"}
          }
        }]
      }
      ```

   3. And this **permissions policy**:

      ```json
      {
        "Version": "2012-10-17",
        "Statement": [
          {
            "Effect": "Allow",
            "Action": ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition", "ecs:RunTask",
                       "ecs:DescribeTasks", "ecs:UpdateService", "ecs:DescribeServices"],
            "Resource": "*"
          },
          {
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": ["arn:aws:iam::<account>:role/queue-service-exec-<env>",
                         "arn:aws:iam::<account>:role/queue-service-task-<env>"]
          }
        ]
      }
      ```

   **If your AWS account can't create IAM roles or identity providers** (e.g. AWS Academy Learner Lab): use the provided role (e.g. `LabRole`) as the execution and task role, and give GitLab temporary access keys instead (B2, alternative). Those keys expire with the lab session, so update them in GitLab each session.

### A4. CloudWatch log groups

Create `/ecs/queue-service/<env>/api`, `/admitter`, `/consumer` and `/migrate`, each with a retention period (e.g. 30 days).

### A5. ECS cluster

ECS → Clusters → Create cluster → name `queue-service-<env>`, infrastructure **AWS Fargate**. Leave Container Insights off (it's billed; the default ECS metrics are free).

### A6. Task definitions (4)

ECS → Task definitions → Create new task definition (or "Create with JSON"). All four share these settings:

| Setting | Value |
| --- | --- |
| Launch type | AWS Fargate, OS Linux/**X86_64** (the GitLab job builds amd64) |
| Task execution role | `queue-service-exec-<env>` |
| Task role | `queue-service-task-<env>` |
| Container name | `api`, `admitter`, `consumer` or `migrate` (the pipeline relies on these) |
| Image | `registry.gitlab.com/cs302-2026/g2-team5/backend/queue-service/main:<commit sha>` (any image built by `release-image` on `main`; the pipeline replaces it on every deploy) |
| Private registry authentication | on, secret `queue-service/gitlab-registry` |
| Log collection | awslogs, group `/ecs/queue-service/<env>/<name>`, stream prefix `<name>` |
| Stop timeout | 30 s |

Per task definition:

| Family | CPU / memory | Command | Health check | Port |
| --- | --- | --- | --- | --- |
| `queue-api` | 0.5 vCPU / 1 GB | `sh,-c,printf '%s\n' "$JWT_PRIVATE_KEY" > /tmp/jwt_private.pem && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000` | none (the ALB checks `/health/ready`) | 8000 |
| `queue-admitter` | 0.25 vCPU / 0.5 GB | `python,-m,workers.admitter` | `CMD,python,-m,workers.healthcheck,admitter` (interval 10, timeout 5, retries 3, start period 10) | — |
| `queue-consumer` | 0.25 vCPU / 0.5 GB | `python,-m,workers.consumer` | `CMD,python,-m,workers.healthcheck,consumer` (interval 15, timeout 5, retries 3, start period 20) | — |
| `queue-migrate` | 0.25 vCPU / 0.5 GB | `alembic,upgrade,head` | — | — |

(The console's command field takes comma-separated parts, as shown.)

**Configuration per task definition** (under the container's *Environment variables*: "Add environment file" for S3, "ValueFrom" for the secret):

| | api | admitter | consumer | migrate |
| --- | --- | --- | --- | --- |
| Environment file (S3) | `app.env` | `app.env` | `app.env` | **`migrate.env`** |
| Secret `JWT_PRIVATE_KEY` | `jwt-private-key` | — | — | — |

Nothing else is set on the task definitions themselves. Variables set directly on a container would override the file's values, so keep everything in the file.

Full JSON for `queue-api`, as a template for the others:

```json
{
  "family": "queue-api",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "512",
  "memory": "1024",
  "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
  "executionRoleArn": "arn:aws:iam::<account>:role/queue-service-exec-<env>",
  "taskRoleArn": "arn:aws:iam::<account>:role/queue-service-task-<env>",
  "containerDefinitions": [{
    "name": "api",
    "image": "registry.gitlab.com/cs302-2026/g2-team5/backend/queue-service/main:<sha>",
    "repositoryCredentials": {
      "credentialsParameter": "arn:aws:secretsmanager:us-east-1:<account>:secret:queue-service/gitlab-registry-XXXXXX"
    },
    "essential": true,
    "stopTimeout": 30,
    "command": ["sh", "-c", "printf '%s\\n' \"$JWT_PRIVATE_KEY\" > /tmp/jwt_private.pem && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000"],
    "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
    "environmentFiles": [
      {"type": "s3", "value": "arn:aws:s3:::<bucket>/queue-service/staging/app.env"}
    ],
    "secrets": [
      {"name": "JWT_PRIVATE_KEY", "valueFrom": "arn:aws:secretsmanager:us-east-1:<account>:secret:queue-service/staging/jwt-private-key-XXXXXX"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/queue-service/staging/api",
        "awslogs-region": "us-east-1",
        "awslogs-stream-prefix": "api"
      }
    }
  }]
}
```

Copy the exact secret ARN from Secrets Manager: it ends in a random 6-character suffix (`-XXXXXX`). The other three task definitions are the same shape without `portMappings` and `secrets` (and with `migrate.env` for `queue-migrate`).

### A7. Load balancer

1. EC2 → Target groups → Create: target type **IP addresses**, protocol HTTP, port **8000**, the Queue VPC. Health check path **`/health/ready`**, healthy 2, unhealthy 3, timeout 3 s, interval 10 s. Don't register targets (ECS does). Under Attributes, set deregistration delay to 30 s.
2. EC2 → Load balancers → Create **Application Load Balancer**: internet-facing (or internal behind your API gateway), the subnets, `queue-lb-<env>-sg`, listener HTTP 80 (or HTTPS 443 with an ACM certificate) forwarding to the target group.
3. Routing: buyers must only reach `/api/v1/*`. `/internal/v1/*` and `/.well-known/jwks.json` are for other services inside the VPC (e.g. a second, internal ALB, or a listener rule restricted by source). Whatever fronts `/api/v1/*` must set `X-User-Sub` itself and never pass on a client-supplied one.
4. Give the **Reservation team** the JWKS URL, e.g. `http(s)://<alb-dns>/.well-known/jwks.json`.

### A8. Run the first migration (once, by hand)

The services need the tables, so migrate before creating them. ECS → Clusters → `queue-service-<env>` → Tasks → **Run new task**: launch type Fargate, task definition `queue-migrate` (latest), the subnets, `queue-app-<env>-sg`, public IP as in A1.

It should stop with **exit code 0**, and its log should end with `Running upgrade 0003 -> 0004`. In the database, `SHOW TABLES;` gives `alembic_version` and `queue_configs`.

### A9. Create the three services (once)

ECS → Clusters → `queue-service-<env>` → Services → Create, each with launch type Fargate, the subnets, `queue-app-<env>-sg`, public IP as in A1, and **"Use the deployment circuit breaker" + "Rollback on failures"** turned on:

| Service | Task definition | Desired tasks | Deployment: min healthy % / max % | Load balancing |
| --- | --- | --- | --- | --- |
| `queue-consumer` | `queue-consumer` | 1 | 100 / 200 | none |
| `queue-admitter` | `queue-admitter` | **1** | **0 / 100** | none |
| `queue-api` | `queue-api` | 2 | 100 / 200 | the ALB and target group from A7, container `api:8000`, health check grace period 30 s |

- **Admitter: 0 / 100, 1 task, never autoscaled.** With ECS's default (100 / 200) the new admitter starts before the old one stops, so two would run on every deploy. With 0 / 100 admission pauses for a few seconds per deploy instead.
- Turn on **ECS Exec** for each service (CLI: `--enable-execute-command`; console: under Troubleshooting configuration).
- Create `queue-consumer` once our four queues exist on the broker (A10). If one is missing it stays unhealthy, logging `rabbitmq queue missing or not accessible`.

CLI equivalent for the admitter:

```bash
aws ecs create-service --region us-east-1 --cluster queue-service-staging \
  --service-name queue-admitter --task-definition queue-admitter --desired-count 1 \
  --launch-type FARGATE --enable-execute-command \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-aaa,subnet-bbb],securityGroups=[sg-xxx],assignPublicIp=DISABLED}" \
  --deployment-configuration "minimumHealthyPercent=0,maximumPercent=100,deploymentCircuitBreaker={enable=true,rollback=true}"
```

### A10. RabbitMQ: what the broker team creates

The consumer only reads from our four queues. Ask the broker team for:

1. **A user** (and vhost) for Queue, to go into `RABBITMQ_URL`, with **read** permission on our queues only. It needs no configure or write permission:

   | Permission | Regex |
   | --- | --- |
   | configure | `^$` (none) |
   | write | `^$` (none) |
   | read | `^queue-service\..*$` |

   Add write on `^(queue-service\..*|amq\.default)$` only if you want to replay dead letters with `python -m workers.dlq replay`. If the consumer's startup check is refused with `403 ACCESS_REFUSED` instead of passing, the broker wants configure permission for passive checks too; ask for configure on `^queue-service\..*$`.
2. **These queues, bindings and DLQs** (the queue and DLQ names already exist on the broker; check their arguments match), as `app/messaging/topology.py` declares them locally:

   | Queue (quorum, durable) | Bound to | Routing key | Arguments |
   | --- | --- | --- | --- |
   | `queue-service.EventPublished` | `event.events` (topic) | `event.published` | `x-queue-type: quorum`, `x-dead-letter-exchange: ticketto.dlx`, routed to `queue-service.EventPublished.dlq` |
   | `queue-service.EventCancelled` | `event.events` (topic) | `event.cancelled` | same pattern, with its own `.dlq` |
   | `queue-service.EventSoldOut` | `reservation.events` (topic) | `event.sold_out` | same pattern, with its own `.dlq` |
   | `queue-service.OrderConfirmed` | `checkout.events` (topic) | `order.confirmed` | same pattern, with its own `.dlq` |
   | `<each of the above>.dlq` (quorum) | `ticketto.dlx` | whatever key the broker team's dead-lettering uses | `x-queue-type: quorum` |

   Checked 2026-10-21: all queues are quorum, dead-letter exchange `ticketto.dlx`, **`delivery-limit: -1` (unlimited)**. That's fine: the consumer itself dead-letters a message on its 5th failed attempt (reject without requeue), using the broker's `x-delivery-count` header. What must hold is that a message dead-lettered from `queue-service.X` via `ticketto.dlx` actually arrives in `queue-service.X.dlq`; check it once (Part E).
3. The AMQP host, port and TLS details for the URL.

The three exchanges belong to the Event, Reservation and Checkout services; Queue never declares them.

---

## Part B – GitLab setup (once)

### B1. Deploy token, so ECS can pull the image

GitLab project → **Settings → Repository → Deploy tokens → Add token**: name `ecs-pull`, scope **`read_registry`**, no expiry (or a long one). Copy the username and token straight into the `queue-service/gitlab-registry` secret (A2); GitLab shows the token only once.

### B2. CI/CD variables

GitLab project → **Settings → CI/CD → Variables**. Create each one **scoped to its environment** (`staging`, later `prod`), so the same job deploys to different clusters:

| Key | Example value | Scope | Protected | Masked |
| --- | --- | --- | --- | --- |
| `AWS_REGION` | `us-east-1` | all | yes | no |
| `AWS_ROLE_ARN` | `arn:aws:iam::<account>:role/queue-service-gitlab-deploy` | per env | yes | no |
| `ECS_CLUSTER` | `queue-service-staging` | per env | yes | no |
| `ECS_SUBNETS` | `subnet-aaa,subnet-bbb` (comma-separated, no spaces) | per env | yes | no |
| `ECS_SECURITY_GROUP` | `sg-xxx` (`queue-app-<env>-sg`) | per env | yes | no |
| `ECS_ASSIGN_PUBLIC_IP` | `DISABLED` (private + NAT) or `ENABLED` (public subnets) | per env | yes | no |

- **Protected** means only pipelines on protected branches (`main`) can read them. Check `main` is protected: Settings → Repository → Protected branches.
- **Alternative to OIDC** (when you can't create the IAM role in A3.3): instead of `AWS_ROLE_ARN`, add `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN`, all **masked** and protected, and drop the `id_tokens` and token-file lines from the job below. The AWS CLI picks them up on its own.
- No app secrets (database, Redis, RabbitMQ passwords, JWT key) go into GitLab.

### B3. Environments

GitLab project → **Operate → Environments**: `staging` and `prod` are created automatically the first time a job names them. Optionally protect `prod` (Settings → CI/CD → Protected environments) so only maintainers can deploy there.

### B4. The deploy job

Two files to add to the repo.

`ci/deploy.sh`: registers a new revision of each task definition with the new image, runs the migration and stops if it fails, then rolls the services in order.

```sh
#!/bin/sh
# Deploy $IMAGE to ECS: migrate first, then consumer -> admitter -> api.
set -eu
: "${IMAGE:?}" "${ECS_CLUSTER:?}" "${ECS_SUBNETS:?}" "${ECS_SECURITY_GROUP:?}"
NETWORK="awsvpcConfiguration={subnets=[$ECS_SUBNETS],securityGroups=[$ECS_SECURITY_GROUP],assignPublicIp=${ECS_ASSIGN_PUBLIC_IP:-DISABLED}}"

# Copy the latest revision of a task definition with the new image; print its ARN.
register() {
  aws ecs describe-task-definition --task-definition "$1" --query taskDefinition > td.json
  python3 - <<'EOF'
import json, os
td = json.load(open("td.json"))
for key in ("taskDefinitionArn", "revision", "status", "requiresAttributes", "compatibilities",
            "registeredAt", "registeredBy", "deregisteredAt"):
    td.pop(key, None)
for container in td["containerDefinitions"]:
    container["image"] = os.environ["IMAGE"]
json.dump(td, open("td-new.json", "w"))
EOF
  aws ecs register-task-definition --cli-input-json file://td-new.json \
    --query taskDefinition.taskDefinitionArn --output text
}

echo "== migrate"
TASK=$(aws ecs run-task --cluster "$ECS_CLUSTER" --launch-type FARGATE \
  --task-definition "$(register queue-migrate)" --network-configuration "$NETWORK" \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER" --tasks "$TASK"
CODE=$(aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$TASK" \
  --query 'tasks[0].containers[0].exitCode' --output text)
if [ "$CODE" != "0" ]; then
  echo "migration failed (exit code $CODE): see CloudWatch /ecs/queue-service/<env>/migrate"
  exit 1
fi

for name in consumer admitter api; do
  echo "== queue-$name"
  aws ecs update-service --cluster "$ECS_CLUSTER" --service "queue-$name" \
    --task-definition "$(register "queue-$name")" > /dev/null
  aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "queue-$name"
done
echo "== deployed $IMAGE"
```

Append to `.gitlab-ci.yml`, in the existing `deploy` stage:

```yaml
.deploy:
  stage: deploy
  image: python:3.12-slim
  needs: [release-image]
  variables:
    IMAGE: "$CI_REGISTRY_IMAGE/$CI_COMMIT_REF_SLUG:$CI_COMMIT_SHA"  # what release-image pushed
  id_tokens:
    AWS_ID_TOKEN:
      aud: sts.amazonaws.com
  before_script:
    - python -m pip install --quiet awscli
    # The AWS CLI assumes $AWS_ROLE_ARN with this GitLab OIDC token on its own.
    - echo "$AWS_ID_TOKEN" > /tmp/web-identity-token
    - export AWS_WEB_IDENTITY_TOKEN_FILE=/tmp/web-identity-token AWS_DEFAULT_REGION="$AWS_REGION"
    - aws sts get-caller-identity
  script:
    - sh ci/deploy.sh

deploy-staging:
  extends: .deploy
  environment:
    name: staging
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH   # every merge to main

deploy-prod:
  extends: .deploy
  environment:
    name: prod
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
      when: manual                                  # click to promote the same image
  needs: [release-image, deploy-staging]
```

`deploy-prod` only appears once you add the `prod` variables and resources. Until then, leave it out.

---

## Part C – Every release (what the pipeline does)

1. A merge to `main` runs static analysis, the integration tests and `release-image`, which pushes `registry.gitlab.com/…/queue-service/main:<sha>`.
2. `deploy-staging` runs `ci/deploy.sh`:
   1. registers a new `queue-migrate` revision with that image, runs it and **stops the deploy if it exits non-zero**;
   2. for `queue-consumer`, `queue-admitter`, `queue-api` in that order: registers a new revision with the image, updates the service, and waits until it's stable.
3. For prod: click **deploy-prod** on the same pipeline. It deploys the **same image** that staging got.

Rules for migrations: keep each one compatible with the previous release (old tasks run until the roll finishes), prefer instant/in-place MySQL changes, and never downgrade in prod; fix forward with a new migration.

**Rollback:** ECS → the service → Update → pick the previous task definition revision (or re-run the deploy job of an older pipeline). Don't roll back the schema.

**Smoke check** after a deploy:

```bash
curl -fsS http://<alb-dns>/health/ready             # {"status":"ready"}
curl -fsS http://<alb-dns>/.well-known/jwks.json    # contains your JWT_KID
aws ecs describe-services --cluster queue-service-staging \
  --services queue-admitter queue-consumer queue-api \
  --query 'services[].{name:serviceName,running:runningCount,desired:desiredCount}'
```

The admitter must show `running: 1`.

---

## Part D – Operations

| Task | How |
| --- | --- |
| Logs | CloudWatch → Log groups → `/ecs/queue-service/<env>/<name>` |
| Shell in a running task | `aws ecs execute-command --cluster queue-service-<env> --task <task-id> --container api --interactive --command /bin/sh` |
| Dead letters | ECS Exec into a **consumer** task (it has the RabbitMQ URL): `python -m workers.dlq list`, then `peek <queue>` / `replay <queue>` |
| Re-run a migration by hand | Run new task → `queue-migrate` (as in A8) |
| Change a URL or setting | edit the `.env` in S3 (versioning keeps the old one), then ECS → each service → Update → **Force new deployment** |
| Redis | Session Manager on the Redis instance; RedisInsight on `http://<redis-public-ip>:5540` from My IP only |
| Rotate the JWT key | put the new PEM in the `jwt-private-key` secret, change `JWT_KID` in `app.env`, force a new deployment of `queue-api`. Tokens signed with the old key stop validating at once, so do it outside a sale (smooth rotation needs code change B) |
| Scale | `queue-api`: desired count or autoscaling (CPU ~60%). `queue-consumer`: 1–2. **`queue-admitter`: never above 1.** |

Alarms worth setting (CloudWatch):

| Alarm | Source |
| --- | --- |
| admitter running count ≠ 1 for > 1 min | ECS service events / `aws ecs describe-services` (`runningCount`) |
| consumer running count < desired | ECS |
| log lines `slow admitter tick`, `admission_integrity_mismatch`, `poison message dead-lettered` | metric filters on the log groups |
| ALB 5xx and target response time | ALB |
| Redis instance CPU and status check | EC2 |

---

## Part E – Verify staging, then go live

1. **Health:** the smoke check above passes, and every service shows running = desired.
2. **Event flow:** with the Event team, publish a test `EventPublished` with `release_at` ~20 minutes ahead and a short `sale_ends_at`. `GET /internal/v1/events/{id}/queue` should show it as SCHEDULED, then OPEN, then ADMITTING.
3. **Buyer flow and load:** join, poll and get admitted through the ALB; run `uv run python -m scripts.load_smoke --buyers <expected peak>` against staging. The pool must never exceed capacity.
4. **Admitter deploy during admission:** re-run the deploy job while an event is admitting. Expect a pause of a few seconds, no double admission, no lost buyers.
5. **Failure drills:** restart the Redis container (readiness goes 503, then recovers with data intact); stop the admitter task (ECS replaces it).
6. **Dead-lettering works on the shared broker:** in the management UI, open `queue-service.EventPublished` → **Publish message** with payload `not json`. The consumer logs `poison message dead-lettered`, and the message shows up in `queue-service.EventPublished.dlq` (Get messages). If it vanishes instead, `ticketto.dlx` isn't routing to our DLQ; tell the broker team.
7. **Reservation** validates a staging token against the staging JWKS.
8. **Prod:** repeat Parts A and B for `prod` (new JWT key, prod secrets), then click `deploy-prod`. Deploy **well before** the first event's `queue_opens_at`: Queue only learns about events from `EventPublished`, so ask the Event team to republish (or send `EventUpdated` with a higher version) for events published before the consumer was running.
