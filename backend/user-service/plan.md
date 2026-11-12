# Plan: deploy user-service to AWS

**Account** `708677596839` · **Region** `us-east-1` · **Updated** 2026-10-25

## Goal

Run user-service on ECS Fargate, reachable by the frontend through the existing API Gateway, with:

1. ~~its own ECS cluster~~ the shared **`TicketPlatform`** cluster, with the other services (changed 2026-10-28; it was `user-service-cluster`);
2. **its own database `db_user`** on the existing shared RDS instance `db-ticketing`;
3. the infrastructure written as **OpenTofu** in [`deploy/terraform/`](deploy/terraform/), following the same pattern as rabbitmq-service and reservation-service;
4. GitLab CI deploying every merge to `main` once the first manual deploy works.

## Follow-ups (come back to these)

- [ ] **Cognito groups can't reach the services through API Gateway (team-wide).** HTTP APIs can't map `cognito:groups` because of the colon, so `X-User-Groups` is stripped for now. Fix: a Cognito pre-token-generation Lambda that adds a colon-free `groups` claim. Then change the mapping in `deploy/terraform/main.tf` to `"overwrite:header.X-User-Groups" = "$context.authorizer.claims.groups"`. Raise with the team: who owns the Lambda, and event-service needs the same mapping. Details in §8. **Full plan: [cognito-groups-plan.md](cognito-groups-plan.md).**
- [x] Decision C: Cognito group `organiser` vs `organisers`. Done: the pool now has only `organiser` (checked 2026-10-25).
- [ ] Raise the open RDS security group (0.0.0.0/0) with the team (§8).
- [ ] **Worker for notifications (2026-10-25).** The new `user-service-worker` ECS service consumes `OrderConfirmed` / `EventCancelled`. Before deploying: rabbitmq-service `tofu apply` (creates `rabibi/service/user-service`, and allows the `user-service` SG on 5672), then `tofu apply` here, then add `ECS_WORKER_SERVICE` and `RABBITMQ_SECRET_ARN` to the GitLab variables. Details in [deploy/README.md](deploy/README.md#the-worker-rabbitmq-consumer).

The repo-wide `infra/` folder is **not** used. It describes a different, never-applied design (its own VPC, ALBs and RDS) that would clash with what the team has built.

---

## 1. What already exists (checked 2026-10-24 with read-only AWS CLI calls)

| Area | What's there | How this plan uses it |
|---|---|---|
| VPC | Only the **default VPC** `vpc-0f6186a17c195aecd`. Six public subnets (1a–1f), internet gateway, **no NAT gateway, no VPC endpoints** | Looked up. Tasks run in 1a/1b/1c |
| RDS | `db-ticketing`: MySQL 8.4.9, db.t3.micro, **stopped**, publicly accessible, master password in Secrets Manager `rds!db-b00d7124-…` | Looked up. We add a database inside it |
| RDS security group | The default group `sg-0b4b57071a37498e5`: 3306 from queue-service and reservation-service, **and all traffic from 0.0.0.0/0** (see §8) | We add one rule: 3306 from user-service |
| ECS clusters | `TicketPlatform` (reservation-service), `queue-service-staging` | user-service runs in `TicketPlatform` since 2026-10-28 |
| OpenTofu state | Bucket `rabibi-tfstate-708677596839`, holding `rabbitmq-service/` and `reservation-service/` state | Our state goes in `user-service/terraform.tfstate` |
| IAM | GitLab OIDC provider `gitlab.com` (created by rabbitmq-service) | Looked up |
| Cognito | Pool `us-east-1_2g8EYCEa1`, client `ticketto` `2jr04pk573rr5pe95jm0chv47o`, one group **`organisers`** (1 member) | Looked up (see decision C) |
| API Gateway | HTTP API **`api`** `x0vxfeenrl`, stage `$default` (auto-deploy), JWT authorizer `302-proj-cognito` `9ky1ji`. **No routes** (we deleted the unused `ANY /user-service` on 2026-10-25) | Looked up. We add our routes |
| ALB / VPC link / Cloud Map | None | We create a VPC link and Cloud Map |
| user-service resources | None (no ECR repo, roles, secret, log group, security group) | All created |

**Couldn't check:** whether `db_user` already exists inside MySQL, because RDS is stopped. `create-database.sh` handles both cases. Also unknown: whether the GitLab project has a runner.

---

## 2. Decisions

| | Decision | Status |
|---|---|---|
| A | API Gateway reaches the tasks through ~~a VPC link + Cloud Map~~ **the team's shared VPC link and internal ALB**, like the other services (changed 2026-11-04, see §10). Cloud Map stays for notification-service's calls to `/internal/v1` | In the code |
| B | Tasks run in public subnets **with a public IP** (no NAT gateway, which would cost about $35/month). Their security group admits only the shared ALB and other services' `/internal/v1` callers | In the code |
| C | Cognito group name: user-service and event-service both check **`organiser`**, but the pool has **`organisers`**. Recommended: create `organiser` and move the one member | **Waiting for you** (Phase 5) |
| D | Names: cluster `TicketPlatform` (shared), services `user-service` and `user-service-worker`, namespace `ticketto.local` | In the code |
| E | Delete the unused `ANY /user-service` route | **Done** 2026-10-25 |
| F | First deploy from the laptop, then switch on CI | Phases 3–6 |
| G | Use OpenTofu, per service, with shared state bucket | **Done**: `deploy/terraform/` |

---

## 3. What OpenTofu creates (`tofu plan`: 29 to add, 0 to change, 0 to destroy)

Every resource is tagged `Project=ticketto`, `Component=user-service`, `ManagedBy=opentofu`.

| Resource | Name | Notes |
|---|---|---|
| Security group | `user-service` | Inbound **8000 from the shared ALB** (rule owned by the gateway stack) and other services' `/internal/v1` callers. Outbound anywhere |
| Rule on the RDS group | — | 3306 from `user-service` |
| Random password + secret | `user-service/db` | `{username, password, host, port, dbname}` |
| ECR repository + lifecycle | `user-service` | Scan on push, keep the last 20 images |
| Log group | `/ecs/user-service` | 30 days |
| IAM role (execution) | `user-service-execution` | Pull the image, write logs, read `user-service/db` |
| IAM role (task) | `user-service-task` | The three Cognito admin calls in `app/cognito.py` |
| IAM role (GitLab deploy) | `gitlab-user-service-deploy` | Only `main` of `cs302-2026/g2-team5/backend/user-service`. Push to this ECR repo, run tasks in this cluster, update this service |
| ECS cluster | `TicketPlatform` (shared; looked up, not created or changed) | Fargate and Fargate Spot. Container Insights off |
| Cloud Map namespace | `ticketto.local` | Private DNS in the default VPC. **Shared**: see §7 |
| Cloud Map service | `user-service` | A + SRV records |
| Task definition | `user-service` (first revision) | From `deploy/task-definition.json`. Afterwards the pipeline registers the revisions |
| ECS service | `user-service` | **0 tasks at first**. Attached to the shared ALB's target group. Deployment minimum 100%, maximum 200%. Circuit breaker + rollback. OpenTofu ignores its task definition |
| API integration + 2 routes | `ANY /api/v1/users`, `ANY /api/v1/users/{proxy+}` | Through the shared VPC link to the ALB's listener 8083. JWT authorizer. Sets `X-User-Sub`, `X-User-Username` and `X-User-Groups` from the token |

**Not in OpenTofu:**
- **the database and MySQL user**: [`deploy/create-database.sh`](deploy/create-database.sh), Phase 2;
- **images and task definition revisions**: the pipeline, or Phases 3–4 the first time;
- **the Cognito group**: decision C.

---

## 4. Steps

### Phase 1: Create the infrastructure (RDS can stay stopped)

```bash
cd backend/user-service/deploy/terraform
tofu init -backend-config=backend.hcl   # backend.hcl is already filled in (gitignored)
tofu plan                               # expect: 29 to add, 0 to change, 0 to destroy
tofu apply                              # type "yes" to confirm
```
After this, everything exists, but the service runs 0 tasks.

### Phase 2: Create the database

1. **Tell the team, then start RDS** (shared; about 5 minutes):
   ```bash
   aws rds start-db-instance --db-instance-identifier db-ticketing
   aws rds wait db-instance-available --db-instance-identifier db-ticketing
   ```
2. **Create the database and user** with `cd backend/user-service && ./deploy/create-database.sh`. It reads both passwords from Secrets Manager and never prints them, creates `db_user` and `user_service`, then signs in as `user_service` to check. It connects from your laptop, which works today because of the open 0.0.0.0/0 rule (§8). If that rule is removed, add a temporary 3306 rule for your own IP.

### Phase 3: First image and migration (from the laptop, Docker Desktop running)

From `backend/user-service`:
 
```bash
# Load the 12 deploy settings from OpenTofu into this terminal
eval "$(tofu -chdir=deploy/terraform output -json gitlab_ci_variables | jq -r 'to_entries[] | "export \(.key)=\(.value)"')"

aws ecr get-login-password | docker login --username AWS --password-stdin "${ECR_REPOSITORY_URI%%/*}"
export IMAGE=${ECR_REPOSITORY_URI}:$(git rev-parse HEAD)
docker buildx build --platform linux/amd64 --target runtime \
  -t "$IMAGE" -t "${ECR_REPOSITORY_URI}:latest" --push .

bash scripts/ci/ecs-migrate.sh      # registers a revision, runs `alembic upgrade head`, must say "Migration succeeded"
```

### Phase 4: Start the service

1. Create `deploy/terraform/terraform.tfvars` containing `desired_count = 1`, then run `tofu -chdir=deploy/terraform apply`. One task starts on the first revision (`:latest`).
2. Switch it to the exact revision from Phase 3, the same way the pipeline will:
   ```bash
   set -a; source deploy.env; set +a  # loads TASK_DEFINITION_ARN written by ecs-migrate.sh
   bash scripts/ci/ecs-deploy.sh      # must say "Deployed ..."
   rm -f deploy.env task-definition.rendered.json
   ```

### Phase 5: Cognito group (decision C, once you confirm)

```bash
aws cognito-idp create-group --user-pool-id us-east-1_2g8EYCEa1 --group-name organiser --description "Event organisers"
# then move the one member of `organisers` into `organiser`, and delete `organisers` once nothing uses it
```

### Phase 6: Switch on CI

1. Run `tofu -chdir=deploy/terraform output -json gitlab_ci_variables` and add each of the 12 values in GitLab → Settings → CI/CD → Variables, each one **Protected** with environment scope **`production`**.
2. Uncomment `build-and-push`, `.ecs`, `migrate` and `deploy` in `.gitlab-ci.yml`, which `d09f55f` switched off. Merge to `main` and watch the pipeline go green.

---

## 5. Code changes in this repo

| File | Change | Status |
|---|---|---|
| `deploy/terraform/*.tf`, `backend.hcl.example`, `terraform.tfvars.example` | The infrastructure | Done, validated, planned |
| `deploy/create-database.sh` | Creates `db_user` and `user_service` from the secrets | Done |
| `scripts/ci/ecs-migrate.sh` | `assignPublicIp=DISABLED` → `ENABLED` (no NAT: the task couldn't pull its image) | Done |
| `.gitignore` | OpenTofu state, `.terraform/`, `backend.hcl`, `terraform.tfvars` | Done |
| `.gitlab-ci.yml` | Uncomment the deploy jobs | Phase 6 |
| `deploy/README.md`, `IMPLEMENTATION_NOTES.md` | Replace the console checklist with a pointer to `deploy/terraform`, update the status | After the first deploy works |

---

## 6. Checking it works

1. **ECS:** `aws ecs describe-services --cluster TicketPlatform --services user-service` shows `runningCount 1` and rollout `COMPLETED`.
2. **Logs:** `aws logs tail /ecs/user-service --follow` shows no DB or Cognito errors.
3. **Through API Gateway**, using `BASE=$(tofu -chdir=deploy/terraform output -raw api_base_url)`:
   - `curl $BASE/api/v1/users/me` with no token → **401**;
   - get an access token with `aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH …`. The client has a secret, so pass `SECRET_HASH`;
   - `GET /api/v1/users/me` → **404** for a new user, `POST /api/v1/users/me` → **201**, then `GET` → **200**;
   - **spoof test:** send `X-User-Groups: organiser` with a buyer's token. The response must still say buyer;
   - `curl $BASE/internal/v1/users/…` → **404** (no route);
   - test with a user in **no** group, to confirm the `cognito:groups` mapping doesn't break the request.
4. **Direct access is blocked:** `curl http://<task public IP>:8000/health` times out.

## 7. Rollback and teardown

- **A bad release:** the circuit breaker rolls back by itself, and `ecs-deploy.sh` fails the pipeline. To roll back by hand: `aws ecs update-service --cluster TicketPlatform --service user-service --task-definition user-service:<previous>`. Migrations aren't rolled back automatically.
- **Pause to save money:** `./deploy/service.sh stop`. Start again with `./deploy/service.sh start`, and check with `./deploy/service.sh status`. The script sets `desired_count` in `terraform.tfvars` and applies it. The shared RDS isn't touched.
- **Full teardown:**
  1. `DROP DATABASE db_user; DROP USER 'user_service'@'%';` as `admin`;
  2. set `allow_destroy_data = true`, `tofu apply`, then `tofu destroy`. This removes only what's in §3. RDS, Cognito, the API and the OIDC provider are only looked up, so they stay.
  3. **Shared namespace:** once another service registers in `ticketto.local`, run `tofu state rm 'aws_service_discovery_private_dns_namespace.this[0]'` and set `create_service_discovery_namespace = false` before destroying. Otherwise the destroy fails.

## 8. Risks and things to raise with the team

- **The RDS instance is open to the internet.** It's publicly accessible, and its security group allows **all traffic from 0.0.0.0/0**. Recommendation: remove that rule. The explicit rules for queue-service, reservation-service and user-service keep those working. Agree it with the team first.
- **RDS is stopped and shared.** Running it costs about $13/month plus storage. Agree who starts and stops it.
- **`X-User-Groups` can't come from API Gateway (team-wide).** HTTP APIs reject any claim name with a colon, so `$context.authorizer.claims.cognito:groups` can't be mapped (tested 2026-10-25 with four spellings). user-service strips the header for now, and loses only its `token_refresh_required` hint. **event-service's organiser check will reject everyone** behind the gateway until this is fixed. Proposed fix: a Cognito pre-token-generation Lambda (the pool is on the Essentials tier, which supports it) that adds a colon-free claim such as `groups: "organiser"`. Each service then maps `$context.authorizer.claims.groups`, a mapping API Gateway accepts. This needs a team decision on who owns the Lambda.
- **Spoofed identity headers:** user-service trusts `X-User-*`. That's safe only while the `user-service` security group admits nothing but the shared ALB (whose listeners admit only the shared VPC link) and other services' `/internal/v1` callers.
- **CORS:** the API has no CORS settings. A browser on another origin will need them, or the `OPTIONS` preflights hit the JWT authorizer and fail.
- **Shared API and state bucket:** only add or remove user-service's own routes, and never touch other services' state keys.

## 9. Rough cost added by user-service

| Item | ≈ per month |
|---|---|
| Fargate, 1 task × 0.25 vCPU / 0.5 GB | $9 |
| Public IPv4 for the task | $3.60 |
| Secret, Cloud Map, ECR storage, logs | ~$1 |
| API Gateway, shared ALB (already running; low traffic) | ~$0 |
| **Total** | **≈ $14**, plus the shared RDS once it's running |

## 10. Move to the shared ALB (2026-11-04)

The gateway stack (`frontend/deploy/gateway`) owns the listener, target group and security group rules. This stack attaches the service and repoints the integration. Each step can be checked before the next, so requests keep working throughout.

| Step | Where | Command | Check |
|---|---|---|---|
| 1. Create listener 8083 and target group `ticketto-user-api` | `frontend/deploy/gateway` | `tofu apply` | Plan only adds resources |
| 2. Attach the service to the target group | `deploy/terraform` | `tofu apply -target=aws_ecs_service.service` | The new task is `healthy` in the target group, and the rollout is `COMPLETED` |
| 3. Send API Gateway through the ALB | `deploy/terraform` | `tofu apply -target=aws_apigatewayv2_integration.service` | The checks in §6 pass |
| 4. Remove the old VPC link and its security group | `deploy/terraform` | `tofu apply` | Plan only destroys `user-service-vpclink`, its 2 rules and the VPC link |

To go back before step 4: revert this code and run `tofu apply -target=aws_apigatewayv2_integration.service`, which points the integration at the old VPC link again.

## 11. Internal port 8001 (code-review finding #1)

notification-service's worker was let in on port 8000, which also serves the `/api/v1` routes that trust `X-User-*`, so it could act as any user. Now the API also listens on **8001** (`INTERNAL_PORT`), which serves only `/internal/v1` and `/health`, and the worker is let in there instead.

| Step | Where | Command | Check |
|---|---|---|---|
| 1. Deploy user-service with 8001 | here | Merge `feature/internal-port` | Pipeline green. Nothing can reach 8001 yet |
| 2. Let the worker in on 8001 too | notification-service `deploy/terraform` | `tofu apply -var 'user_service_ports=[8000,8001]'` | Plan: the 8000 rule moves, 1 to add, 0 to destroy |
| 3. Point the worker at 8001 | notification-service | GitLab variable `USER_SERVICE_URL` = `http://user-service.ticketto.local:8001`, then merge `feature/user-service-internal-port` | The worker's calls to `/internal/v1` succeed (user-service log, `"path": "/internal/v1/users"`, from 8001) |
| 4. Close 8000 to the worker | notification-service `deploy/terraform` | `tofu apply` | Plan: 1 to destroy (the 8000 rule) |
