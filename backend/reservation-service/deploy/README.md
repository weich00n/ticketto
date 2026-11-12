# Deploying reservation-service to AWS

The GitLab pipeline (`.gitlab-ci.yml`) builds every commit on `main` and deploys it to ECS on Fargate, the same way event-service and user-service do:

1. **maven-tests** runs the whole suite against Redis, MySQL and RabbitMQ started as GitLab services. SAST, secret detection, dependency scanning and Terraform scanning run beside it.
2. **build-and-push** builds the `runtime` image and pushes it to ECR as `:<commit SHA>` and `:latest`.
3. **deploy** (automatic on `main`, script inline in `.gitlab-ci.yml`):
   - fills in [`task-definition.json`](task-definition.json) and registers a new revision;
   - points the ECS service at it and waits until it is stable;
   - fails if ECS rolled it back to the previous version.

There is no separate migration task: Flyway migrates the schema when the service starts. The hold-expiry sweep runs inside the service, so there is no separate worker either.

GitLab signs in to AWS with **OIDC**, so no AWS keys are stored in GitLab. The region is `us-east-1` throughout.

## The AWS side: `deploy/terraform`

The OpenTofu stack in [`terraform/`](terraform/) creates everything this service owns, and only looks up what other people own.

**Looked up, never changed:** the default VPC (`172.31.0.0/16`, no NAT), the team's `TicketPlatform` cluster, the RDS instance `db-ticketing`, the Redis instance's role and profile `redisReservationDefault`, the broker secret `rabibi/service/reservation-service`, and the account's GitLab OIDC provider.

**Created:**
- the Redis instance `reservation-Redis-v1.1` and its security group `reservation-redis` ([`terraform/redis.tf`](terraform/redis.tf)), created by hand and imported. Never accept a plan that replaces the instance;
- security group `reservation-service`: 8081 only from `client_security_group_ids`. It also adds a rule to RDS's group (3306) and to `reservation-redis` (6379), each from this group only;
- ECR repository `reservation-service` (last 20 images) and log group `/ecs/reservation-service` (14 days);
- secrets `reservation-service/db` (a MySQL user with a generated password), `reservation-service/redis` and `reservation-service/admission-public-key`. The last two are created empty: their values come from elsewhere (below);
- the environment file `s3://reservation-service-config-<ACCOUNT_ID>/production.env`, written from the looked-up RDS endpoint and Redis IP. It holds addresses only;
- an inline policy `read-redis-password` on the Redis instance's own role, so the box can read `reservation-service/redis` at launch;
- IAM role `reservation-service-execution` (pull the image, read the four secrets and the env file) and `gitlab-reservation-service-deploy` (push to ECR, register task definitions, update this service; `main` only);
- the first task definition, from [`task-definition.json`](task-definition.json), and the ECS service: Fargate in the default subnets, a public IP for outbound traffic only, deployment circuit breaker with rollback. After the first apply the `deploy` job owns the task definition; tofu ignores it.

Day to day, change things in the `.tf` files and run `tofu plan`, then `tofu apply`. The state lives in `s3://rabibi-tfstate-<ACCOUNT_ID>/reservation-service/terraform.tfstate`.

**Not in the stack:**
- **The broker letting us in.** rabbitmq-service's owner adds the `service_security_group_id` output to their `amqp_allowed_sg_ids`. Without that rule the service still runs, but it neither receives EventPublished/EventCancelled nor publishes; its outbox keeps the messages.
- **The gateway's routes, ALB and target group.** The shared gateway stack owns them (see API Gateway below); this stack only attaches the service to the target group.

## Secrets

Secrets Manager is the only copy. Nothing secret goes in the repository, in GitLab or in local files; read a value from the console when you need it.

| Secret | Value comes from |
|---|---|
| `reservation-service/db` | tofu generates it; `terraform/create-db-user.sh` creates the matching MySQL user |
| `reservation-service/redis` | JSON `{"password": "..."}`, not the bare password: the task reads the `password` key. Set it with `aws secretsmanager put-secret-value --secret-id reservation-service/redis --secret-string "$(jq -n --arg p "$(openssl rand -hex 24)" '{password:$p}')"`. The Redis box reads the same key at launch ([`redis/ec2-user-data.sh`](redis/ec2-user-data.sh)); the stack gives its instance role read access to this one secret |
| `reservation-service/admission-public-key` | Queue's admission-token public key as **plain text**, the whole PEM (`-----BEGIN PUBLIC KEY-----`), not JSON. Set it with `--secret-string file://queue-public.pem`. Not sensitive. Without it the service refuses to start |
| `rabibi/service/reservation-service` | rabbitmq-service's stack: this service's own least-privilege broker login. The task reads only its `url` key |

## GitLab CI/CD variables

`tofu output gitlab_ci_variables` prints all eleven. Set them under **Settings → CI/CD → Variables**, each **protected**, with environment scope `production` (both `build-and-push` and `deploy` run in that environment). None of them is a password.

The database and Redis addresses are not GitLab variables: they live in the S3 environment file. Keep it that way, since `DB_HOST` and `REDIS_HOST` set in GitLab settings would override the test job's own values in `.gitlab-ci.yml` and send it to production.

## First deployment

1. `cd deploy/terraform`, copy `backend.hcl.example` to `backend.hcl` (fill in the account ID), then `tofu init -backend-config=backend.hcl` and `tofu apply`. The service starts with 0 tasks, as there's no image yet.
2. `./create-db-user.sh` once. It logs in with the RDS master secret and creates `reservation_service` and the empty `db_reservation`. Flyway creates the tables on the service's first start.
3. Fill in the Redis and admission-key secrets (above).
4. Set the GitLab variables.
5. Push to `main`. `build-and-push` pushes the image and `deploy` rolls it out; every later push to `main` does the same.

The task count is the autoscaler's from the first apply (2 to 4, see Autoscaling), so `desired_count` never needs changing.

## Database migrations

The schema is created and changed by Flyway, from `src/main/resources/db/migration`, when the task starts. Hibernate only validates it (`ddl-auto=validate`) and the task refuses to start if the schema and the entities disagree.

- A database made by an earlier build (with `ddl-auto=update`) is recorded as already at V1, the baseline, the first time a migrating build starts. It then gets V2 onwards. Nothing is dropped or rewritten.
- To change the schema, add a new `V<n>__description.sql`. Never edit a migration that has been applied anywhere: Flyway checks the checksums and refuses to start.
- **Rollback:** rolling back the image does not roll back the schema. Keep migrations backward-compatible (add columns and indexes; drop or rename only in a later release once no running version uses them). Then the previous image runs fine on the newer schema, and redeploying it is the whole rollback. If a migration itself must be undone, write a new forward migration that reverses it.

## API Gateway

The public routes (`GET /api/v1/events/{event_id}/availability` without login; `POST /api/v1/reservations` and `GET` / `DELETE /api/v1/reservations/{hold_id}` behind the Cognito authorizer) are owned by the team's shared gateway stack in the frontend repository (`deploy/gateway`): API Gateway `x0vxfeenrl` ── VPC link ──► internal ALB `ticketto-internal-api` :8081 ──► target group `ticketto-reservation-api`. The gateway overwrites `X-User-Sub` / `X-User-Username` from the token; `/internal/v1/*` and `/health` are not routed.

That stack owns the routes, the ALB, the target group and the rule letting the ALB reach the tasks. This stack only keeps the ECS service attached to the target group (`load_balancer` in `main.tf`, with a 90-second health-check grace period). Don't remove it: an apply without it detaches the service and every public route answers 503. Checkout keeps calling `reservation-service.ticketto.local:8081` through Cloud Map.

## Autoscaling

The service runs 2 to 4 tasks, scaled on average CPU (target 60%, `autoscaling.tf`). Tofu no longer sets the task count: `desired_count` only applies when the service is first created. Each task registers in the team's Cloud Map namespace, so `reservation-service.ticketto.local` resolves to every healthy task inside the VPC.

Chaos experiments against this service (stop a task, reboot its Redis) live in the chaos-engineering repository.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `maven-tests` fails to connect to MySQL in the first seconds | The MySQL service was still starting. Retry. If it keeps happening, the runner may not support `services:` |
| `AssumeRoleWithWebIdentity` / "Not authorized" | The trust policy `sub` doesn't match the project path or branch |
| Variables are empty in a `main` job | `main` isn't protected, or a variable's environment scope isn't `production` |
| Task stopped with `ResourceInitializationError` mentioning S3 or the env file | The execution role lacks `s3:GetObject` / `s3:GetBucketLocation`, the ARN in `ENV_FILE_S3_ARN` is wrong, or the task has no public IP (the default VPC has no NAT) |
| `deploy` says the service rolled back | New tasks failed health checks. Check the stopped reason and `/ecs/reservation-service`. Common causes: an empty admission-key or Redis secret, the MySQL user not created yet (`create-db-user.sh`), or the wrong Redis password (`NOAUTH` / `WRONGPASS` in the logs) |
| Logs say `NOT_FOUND - no queue 'reservation-service.EventPublished'` | The broker is missing `reservation-service.EventPublished` / `.EventCancelled`. rabbitmq-service creates them from `topology/messages.yaml`; this service never declares them |
| Pipeline stuck on "pending" | The GitLab project has no runner available |
| Task stops with `Schema-validation: missing column` or a Flyway checksum error | A migration was edited after it ran, or the entities changed without a new migration |
