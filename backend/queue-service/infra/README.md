# Queue Service infrastructure (OpenTofu)

Creates, per environment, everything the Queue service needs on AWS:

| Created | Referenced only (by ID) | Not touched |
| --- | --- | --- |
| ECS cluster, 4 task definitions (`queue-{api,admitter,consumer,migrate}-<env>`), 3 services (`queue-api`, `queue-admitter`, `queue-consumer`) | VPC, subnets | shared RDS and its security group |
| security group `queue-app-<env>-sg` (+ a 6379 rule on the Redis instance's group) | the Redis EC2 instance's security group | the Redis instance itself |
| S3 bucket for `app.env` / `migrate.env` (private, versioned, encrypted) | | RabbitMQ (central broker) |
| Secrets Manager secrets `jwt-private-key` and `gitlab-registry` (**empty**) | | |
| IAM: task execution role, task role, GitLab OIDC provider + deploy role | | |
| CloudWatch log groups | | |

Buyers reach `queue-api` through the **shared platform API gateway** and the shared internal ALB. Port 8000 accepts only the ALB's security group (a rule owned by the gateway project, not by us), plus `api_ingress_cidrs` for debugging (normally empty). See [API gateway](#api-gateway). `queue-api` runs 2 to 4 tasks, scaled on average CPU (target 60%, `autoscaling.tf`). The consumer runs `consumer_desired_count` tasks and the admitter is always 1.

**No passwords go through OpenTofu.** Secret values and the `.env` files are set with the AWS CLI, so the state file holds no credentials.

State is a **local file**, one per environment via workspaces (`terraform.tfstate.d/<env>/`). It's gitignored; back it up.

## Staging progress (updated 2026-10-22)

| # | Step | Status |
| --- | --- | --- |
| 1 | `staging.tfvars` filled in, `tofu apply` (no services yet) | ✅ done. The account already had a gitlab.com OIDC provider, so `create_gitlab_oidc_provider = false` and `gitlab_oidc_audience = "gitlab.com"` |
| 2 | Secrets: JWT key, GitLab deploy token | ✅ done |
| 3a | `migrate.env` written and uploaded | ✅ done (checked against the app settings before upload) |
| 3b | `app.env` written and uploaded | ✅ done (duplicates removed, `JWT_PRIVATE_KEY_PATH=/tmp/jwt_private.pem`, `LOG_FORMAT=json`, no `RABBITMQ_URL`) |
| 3c | RabbitMQ secret in `staging.tfvars` | ✅ done: `rabbitmq_url_secret_arn` (same account), `rabbitmq_url_secret_json_key = "url"` |
| 4 | RDS allows 3306 from `queue-app-staging-sg` | ✅ done (verified; the temporary home-IP rule is gone) |
| 5 | First migration (`queue-migrate-staging`) exits 0 | ✅ done: exit 0, upgraded to `0004` |
| 6 | The four RabbitMQ queues exist (+ `.dlq`) | ✅ on the broker (EventPublished, EventCancelled, EventSoldOut, OrderConfirmed) |
| 7 | `create_services = true`, `tofu apply` (3 services, 1 task each) | ✅ done with image `…/main:92bf8d6d860b39cb7e5407d6fe89bef8c2fdaab5` (four queues, `X-User-Sub`). Fixes needed on the way: broker security group now allows 5672 from `queue-app-staging-sg`; the shared RDS had been **stopped** and was started again |
| 8 | GitLab CI/CD variables, scoped to `staging` | ✅ done. OIDC login failed (`InvalidIdentityToken` from the account's existing gitlab.com provider), so the deploy job uses **access keys**: `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (masked) plus `AWS_REGION`, `ECS_*`; `AWS_ROLE_ARN` is unused |
| 9 | Commit + push; click **deploy-staging** | ✅ done: deploy-staging ran through (migrate → consumer → admitter → api), services up |
| 10 | Verify staging | ⬜ to do (checks below) |

**Status 2026-10-22:** staging is deployed and running. Next session: step 10. Before that:

- The services may be stopped to save cost (desired count 0). Start them with `tofu apply -var-file=staging.tfvars` (sets counts back to 1) or ECS → service → Update → Desired tasks 1. deploy-staging does **not** start stopped services.
- Check the shared RDS `db-ticketing` is **Available** (it was found stopped once; `/health/ready` then says `["mysql"]`).
- Pending: `tofu apply` for Container Insights → **disabled** in `ecs.tf` (changes only the cluster setting).
- Your Mac's DNS (8.8.8.8) intermittently fails to resolve AWS hostnames (`no such host`). Switch to 1.1.1.1 / 1.0.0.1 before running `tofu` or the AWS CLI.
- Tell the RDS owners: `db-ticketing`'s security group has an inbound **all traffic from 0.0.0.0/0** rule; only the per-service rules are needed.

Update this table as steps are done.

## First-time setup (staging)

Prerequisites: [OpenTofu](https://opentofu.org/docs/intro/install/) ≥ 1.8 (`brew install opentofu`), the AWS CLI logged in to the account, and one image already built by `release-image` on `main`.

Steps 4–5 (database) don't depend on RabbitMQ, so they can be done while waiting for the broker team (steps 3b and 6).

### 1. Create everything except the services

```bash
cd infra
cp staging.tfvars.example staging.tfvars      # fill in VPC, subnets, Redis SG, your IP, image
tofu init
tofu workspace select -or-create staging
tofu apply -var-file=staging.tfvars           # create_services = false at first
tofu output                                    # bucket paths, secret names, GitLab variables
```

### 2. Fill in the two secrets

```bash
# JWT signing key: generated locally, stored, deleted.
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt_private.pem
aws secretsmanager put-secret-value --secret-id queue-service/staging/jwt-private-key \
  --secret-string file://jwt_private.pem
rm jwt_private.pem

# GitLab deploy token (Settings → Repository → Deploy tokens, scope read_registry).
aws secretsmanager put-secret-value --secret-id queue-service/staging/gitlab-registry \
  --secret-string '{"username":"<deploy token username>","password":"<deploy token>"}'
```

### 3. Upload the `.env` files

Write them locally (`*.env` is gitignored), then upload. ECS rules: `NAME=value`, no quotes, no `export`, URL-encode special characters in passwords.

`app.env` (api, admitter, consumer). **No `RABBITMQ_URL` here**: ECS injects it from the broker team's Secrets Manager secret (`rabbitmq_url_secret_arn` in `staging.tfvars`):

```
MYSQL_URL=mysql+asyncmy://queue_app:<pw>@db-ticketing.ckt0mcwoq5s8.us-east-1.rds.amazonaws.com:3306/queue_service
REDIS_URL=redis://:<redis-password>@<redis-private-ip>:6379/0
RABBITMQ_DECLARE_TOPOLOGY=false
JWT_KID=queue-staging-2026-10
JWT_PRIVATE_KEY_PATH=/tmp/jwt_private.pem
ADMITTER_TICK_SECONDS=1
LOG_FORMAT=json
LOG_LEVEL=INFO
```

`migrate.env` (migrate task; Redis and RabbitMQ values only satisfy the settings check):

```
MYSQL_URL=mysql+asyncmy://queue_migrate:<pw>@db-ticketing.ckt0mcwoq5s8.us-east-1.rds.amazonaws.com:3306/queue_service
REDIS_URL=redis://unused:6379/0
RABBITMQ_URL=amqp://unused:5672/%2F
JWT_KID=unused
JWT_PRIVATE_KEY_PATH=/tmp/unused.pem
LOG_FORMAT=json
```

```bash
aws s3 cp app.env     "$(tofu output -json config_bucket | jq -r .app_env)"
aws s3 cp migrate.env "$(tofu output -json config_bucket | jq -r .migrate_env)"
```

### 4. Network access to RDS

Ask the RDS owners to allow **TCP 3306 from** `tofu output app_security_group_id` (or check it's already allowed).

If you added a temporary 3306 rule for your home IP while creating the database users, remove it now.

### 5. Run the first migration

```bash
CLUSTER=$(tofu output -json gitlab_variables | jq -r .ECS_CLUSTER)
SG=$(tofu output -json gitlab_variables | jq -r .ECS_SECURITY_GROUP)
SUBNETS=$(tofu output -json gitlab_variables | jq -r .ECS_SUBNETS)
aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition queue-migrate-staging \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}"
```

It should stop with exit code 0; the log (`/ecs/queue-service-staging/migrate`) ends with `Running upgrade 0003 -> 0004`. Check the exit code:

```bash
aws ecs describe-tasks --cluster "$CLUSTER" --tasks <task-arn-from-run-task> \
  --query 'tasks[0].{status:lastStatus,exit:containers[0].exitCode,reason:stoppedReason}'
```

| Problem | Likely cause |
| --- | --- |
| `CannotPullContainerError` | the `gitlab-registry` secret is wrong, or the subnet has no internet route (public subnet + public IP needed) |
| `ResourceInitializationError` … S3 / secrets | `migrate.env` not uploaded, or wrong path |
| exit 1, log says `Access denied for user` | password in `migrate.env` wrong, or not URL-encoded |
| exit 1, log says `Can't connect` / timeout | RDS security group doesn't allow `queue-app-staging-sg` (step 4) |

### 6. RabbitMQ queues

In the broker GUI (Queues tab), check all four exist in the vhost of `RABBITMQ_URL`, each with its `.dlq`:
`queue-service.EventPublished`, `queue-service.EventCancelled`, `queue-service.EventSoldOut`, `queue-service.OrderConfirmed`.

If one is missing, the consumer doesn't consume from **any** of them: it logs `rabbitmq queue missing or not accessible` and keeps retrying.

### 7. Create the services

Needs `app.env` uploaded (3b), the migration done (5) and the queues (6). Set `create_services = true` in `staging.tfvars`, then:

```bash
tofu apply -var-file=staging.tfvars     # adds queue-consumer, queue-admitter, queue-api (1 task each)
```

They start with the image in `staging.tfvars`. Watch them in ECS → `queue-service-staging` → Services: each should reach 1/1 running. If one keeps restarting, its log group (`/ecs/queue-service-staging/<name>`) says why.

### 8. GitLab CI/CD variables

```bash
tofu output gitlab_variables
```

GitLab → Settings → CI/CD → Variables → add `AWS_REGION`, `ECS_CLUSTER`, `ECS_SUBNETS`, `ECS_SECURITY_GROUP`, `ECS_ASSIGN_PUBLIC_IP` (Masked off) and `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (**Masked**), all with **Environment scope `staging`** and **Protected** on. (`AWS_ROLE_ARN` is only for OIDC, which isn't used: see the progress table.) Check `main` is a protected branch (Settings → Repository → Protected branches); protected variables are only given to protected branches.

### 9. Deploy from GitLab

Commit and push (`infra/`, `ci/deploy.sh`, `.gitlab-ci.yml` and the code changes). On the `main` pipeline, once `release-image` passes, click ▶ on **deploy-staging**. It:

1. logs in to AWS with the access keys (`aws sts get-caller-identity` prints the IAM user)
2. registers new task definition revisions with that pipeline's image
3. runs the migration and stops if it fails
4. updates consumer → admitter → api and waits for each to be stable

The deploy job in `.gitlab-ci.yml`, for reference:

```yaml
.deploy:
  stage: deploy
  image: python:3.12-slim
  needs: [release-image]
  variables:
    IMAGE: "$CI_REGISTRY_IMAGE/$CI_COMMIT_REF_SLUG:$CI_COMMIT_SHA"
  # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY come from CI/CD variables.
  before_script:
    - python -m pip install --quiet awscli
    - export AWS_DEFAULT_REGION="$AWS_REGION"
    - aws sts get-caller-identity
  script:
    - sh ci/deploy.sh

deploy-staging:
  extends: .deploy
  environment:
    name: staging
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
      when: manual
      allow_failure: true
```

| Deploy job error | Cause |
| --- | --- |
| `Unable to locate credentials` | access-key variables missing, not scoped to `staging`, or not given to the job because `main` isn't protected |
| `InvalidClientTokenId` / `SignatureDoesNotMatch` | wrong or deleted access key |
| `ServiceNotFoundException` | step 7 not done |

### 10. Verify staging

If `/health/ready` reports `["mysql"]` or the admitter keeps failing health checks with `Can't connect to MySQL`, first check RDS → `db-ticketing` is **Available** (it is shared and may be stopped). A consumer stuck on `TCP connection attempt timed out` to the broker means the broker's security group doesn't allow `queue-app-staging-sg` on 5672.

1. **API:** ECS → `queue-api` → Tasks → the task → **Public IP**, then from an address in `api_ingress_cidrs`:

   ```bash
   curl http://<public-ip>:8000/health/ready              # {"status":"ready"}
   curl http://<public-ip>:8000/.well-known/jwks.json     # kid = queue-staging-2026-10
   ```

   The IP changes on every deploy (no load balancer).
2. **Admitter:** its service shows 1/1 running, healthy; the log has no repeated errors.
3. **Consumer:** its log shows `consuming` once for each of the four queues.
4. **Dead-lettering:** in the broker GUI, publish `not json` to `queue-service.EventPublished`. The consumer logs `poison message dead-lettered`, and the message appears in `queue-service.EventPublished.dlq`. If it vanishes, `ticketto.dlx` isn't routing to our DLQ; tell the broker team.
5. **Event flow:** with the Event team, publish a test EventPublished (`release_at` ~20 min ahead). `GET /internal/v1/events/{id}/queue` shows SCHEDULED, then OPEN, then ADMITTING; join and poll with `X-User-Sub` until admitted.
6. **Reservation:** give them `http://<public-ip>:8000/.well-known/jwks.json` (or a stable address once there is one) and have them validate a staging token.

## API gateway

The platform team owns the whole path in front of `queue-api` (set up by hand, not in this repo):

```
buyer ──► API Gateway "api" (HTTP API, JWT authorizer)
        ──► VPC link "ticketto-shared-services"
        ──► internal ALB "ticketto-internal-api", Queue's own listener
        ──► target group "ticketto-queue-api" (ip, :8000, health check /health/ready)
        ──► queue-api tasks
```

That path is managed by the gateway's own OpenTofu project (`frontend/deploy/gateway`), including the rule on `queue-app-<env>-sg` that allows :8000 from the ALB's security group. **Don't manage that rule here**: two projects owning one resource overwrite each other. We only reference their target group (`api_target_group_arn`), because the ECS service is ours and must keep it attached.

Their gateway routes only the four buyer endpoints. `POST …/queue/entries` and `GET`/`DELETE …/queue/me` require a JWT, and their integration **overwrites** `X-User-Sub` with the token's `sub` claim. The lobby (`GET /api/v1/events/{event_id}/queue`) needs no login, and its integration removes `X-User-Sub`. `/internal/v1/*`, `/.well-known/jwks.json` and `/health/*` are not exposed. Queue trusts `X-User-Sub` as sent, so keep it that way.

## Prod

Same steps with a `prod` workspace and `prod.tfvars` (`env = "prod"`, `create_gitlab_oidc_provider = false`, `gitlab_oidc_audience = "gitlab.com"`: the account's existing provider), a **new** JWT key (`JWT_KID=queue-prod-…`), prod `.env` files and GitLab variables scoped to `prod`. Then add a `deploy-prod` job like `deploy-staging` with `environment: prod`.

## Changing things later

| Change | How |
| --- | --- |
| A URL, password or setting | edit the `.env` in S3 (versioned), then ECS → service → Update → Force new deployment |
| queue-api scaling | `api_min_count`, `api_max_count`, `api_cpu_target_percent` in tfvars, `tofu apply`. An apply also resets the running queue-api count to `api_min_count`, so don't apply during a sale |
| CPU, memory, health check, counts | edit `ecs.tf`/tfvars, `tofu apply`. Counts apply at once; task definition changes go live with the next GitLab deploy (services ignore task definition changes made here, so `tofu apply` never rolls back the image) |
| Release new code | merge to `main` (the deploy job) |
| Rotate the JWT key | `put-secret-value` on `jwt-private-key`, change `JWT_KID` in `app.env`, force a new deployment of `queue-api` |
