# Deploying ticket-service to AWS

Ticket Service runs on ECS Fargate in the team's `TicketPlatform` cluster as **two services from one image**: the API (`ticket-service`) and the worker (`ticket-service-worker`, command `python -m app.worker`). The region is `us-east-1` throughout.

The GitLab pipeline does the deploying, and signs in to AWS with **OIDC**, so no AWS keys are stored in GitLab:

1. **build-and-push** builds the `runtime` image and pushes it to ECR as `:<commit SHA>` and `:latest`.
2. **migrate** ([`scripts/ci/ecs-migrate.sh`](../scripts/ci/ecs-migrate.sh)) registers new revisions of [`task-definition.json`](task-definition.json) and [`worker-task-definition.json`](worker-task-definition.json), then runs `alembic upgrade head` as a one-off task and fails unless it exits 0.
3. **deploy** ([`scripts/ci/ecs-deploy.sh`](../scripts/ci/ecs-deploy.sh)) points both services at the new revisions and waits, up to 20 minutes, until ECS has finished both rollouts. It fails if either was rolled back or didn't finish, and then prints that service's deployments, recent events and why its recent tasks stopped.
4. **rollback** (manual, [`scripts/ci/ecs-rollback.sh`](../scripts/ci/ecs-rollback.sh)) moves both services back to their previous revision.

Deploy jobs run on `main` only, once the CI/CD variable `DEPLOY_ENABLED` is `true`.

## The AWS side: `deploy/terraform`

The OpenTofu stack in [`terraform/`](terraform/) follows reservation-service's. It creates everything Ticket owns and only looks up what other people own.

**Looked up, never changed:** the default VPC (no NAT), the `TicketPlatform` cluster, the RDS instance `db-ticketing`, the broker secret `rabibi/service/ticket-service` (Ticket's own least-privilege broker login, from rabbitmq-service's stack) and the account's GitLab OIDC provider.

**Created:**
- security group `ticket-service`: 3006 only from `client_security_group_ids` (none by default). It also adds one rule to RDS's group: 3306 from this group;
- ECR repository `ticket-service` (last 20 images) and log group `/ecs/ticket-service` (14 days; API streams under `ticket-service/`, worker under `worker/`);
- secrets `ticket-service/db` (MySQL user `ticket_service` with a generated password), `ticket-service/qr-signing-keys` (`k1:<48 random characters>`) and `ticket-service/ses-smtp` (see Email);
- the Amazon SES email identity `email_sender` and IAM user `ticket-service-ses-smtp`, which can only send mail from that address (see Email);
- IAM role `ticket-service-execution` (pull the image, read the four secrets) and `gitlab-ticket-service-deploy` (push to ECR, register task definitions, run the migration task, update both services; `main` only);
- the first task definitions and both ECS services: Fargate in the default subnets, a public IP for outbound traffic only, deployment circuit breaker with rollback. After the first apply the pipeline owns the task definitions; tofu ignores them.

The state lives in `s3://rabibi-tfstate-<ACCOUNT_ID>/ticket-service/terraform.tfstate`. Day to day, change the `.tf` files, run `tofu plan`, then `tofu apply`.

**Not in the stack:**
- **The broker letting us in.** The broker accepts AMQP (5672) only from the security groups in rabbitmq-service's `amqp_allowed_sg_ids`. Its owner adds this stack's `service_security_group_id` output there and applies their stack; until then the worker can't connect.
- **API Gateway's route in.** The shared gateway stack (frontend repo, `deploy/gateway`) owns the VPC link, the internal ALB, the `ticketto-ticket-api` target group and the rule letting the ALB reach 3006 on this service's group. This stack attaches the API service to that target group (looked up by `gateway_target_group_name`, so it must exist before `tofu apply`), and the API runs with `AUTH_MODE=gateway`. Don't add the ALB's group to `client_security_group_ids`: the gateway stack already has that rule.

## Secrets

Secrets Manager is the only copy. Nothing secret goes in the repository, in GitLab or in local files. The generated values are also in the tofu state, which is encrypted in the team's state bucket.

| Secret | Value comes from |
|---|---|
| `ticket-service/db` | tofu generates it; `terraform/create-db-user.sh` creates the matching MySQL user |
| `ticket-service/qr-signing-keys` | tofu generates `k1:<random>` once and never overwrites it |
| `ticket-service/ses-smtp` | tofu: the SMTP login of IAM user `ticket-service-ses-smtp`. No longer read by the worker (see Email) |
| `rabibi/service/ticket-service` | rabbitmq-service's stack. The worker reads only its `url` key |

**Rotating the QR key:** set the secret to `k2:<new>,k1:<old>` (`aws secretsmanager put-secret-value`) and redeploy. New tickets use `k2`; tickets signed with `k1` keep verifying. Remove `k1` once its events are over.

## Email

Ticket no longer sends email. The worker publishes `TicketIssued` to the broker and notification-service sends the ticket email, with its own SES setup. The worker's task definition has no SMTP settings or secrets any more, and needs no outbound access other than the broker, RDS and (optionally) Event Service.

The stack still creates the SES identity `email_sender`, the IAM user `ticket-service-ses-smtp` and the secret `ticket-service/ses-smtp`, which nothing in Ticket uses now. They are left in place on purpose: removing them is a `tofu apply` that deletes the identity, so first check that notification-service doesn't send from the same address. Then remove them from `terraform/` (and `SMTP_SECRET_ARN` from the CI/CD variables).

## GitLab CI/CD variables

`tofu output gitlab_ci_variables` prints all fifteen. Set them under **Settings → CI/CD → Variables**, each **protected**, with environment scope `production`. None of them is a password. `SMTP_SECRET_ARN` is no longer used by the pipeline (see Email). Then add `DEPLOY_ENABLED` = `true` (protected, scope All).

`EVENT_SERVICE_URL` (from `event_service_url` in `terraform.tfvars`) is the only optional one: the worker uses it to fill in `TicketIssued`'s title, venue and start time when no message carried them. Leave it empty until event-service has a stable private address, and ask its owner to allow TCP 3001 from this service's security group (`tofu output service_security_group_id`).

The names deliberately differ from the app's own variables (`DB_HOST`, `RABBITMQ_URL`) so production values never leak into the integration-test job.

## GitLab sign-in

This project's path, `cs302-2026/g2-team5/backend/ticket-service`, was used by an earlier project, so GitLab won't issue ID tokens whose `sub` claim is built from the path: such jobs fail with `id_token_burned_project_path`. The project's `ci_id_token_sub_claim_components` setting is therefore `["project_id", "ref_type", "ref"]` (Projects API), which makes the `sub` claim `project_id:87547400:ref_type:branch:ref:main`. The deploy role's trust policy (`gitlab_project_id` in `terraform/variables.tf`) matches exactly that, so only `main` of this project can assume it.

## First deployment

1. `cd deploy/terraform`, copy `backend.hcl.example` to `backend.hcl` (fill in the account ID), then `tofu init -backend-config=backend.hcl` and `tofu apply`. Both services start with 0 tasks, as there's no image yet.
2. With `db-ticketing` running, `./create-db-user.sh` once. It logs in with the RDS master secret and creates `ticket_service` and the empty `db_ticket`.
3. Set the GitLab variables.
4. Merge to `main`. The pipeline pushes the image, runs the migration (which creates the tables) and points both services at the new revisions.
5. Set `desired_count = 1` in `terraform/terraform.tfvars` and `tofu apply`. From then on every merge to `main` is deployed.

**To stop paying for compute** (about $26/month for both tasks and their public IPs), set `desired_count = 0` and `tofu apply`. The secrets stay (about $1.20/month).

## Checking a deployment

- CloudWatch `/ecs/ticket-service`: JSON logs; the worker logs `worker started` and `consuming` for each of its queues. A `queue not available on the broker yet` warning means rabbitmq-service doesn't have that queue yet (its `topology/messages.yaml` must list ticket-service as a consumer, then the broker is redeployed); the worker checks again every minute.
- RabbitMQ: `ticket-service.OrderConfirmed`, `.EventCancelled`, `.RefundCompleted` and `.EventPublished` each have one consumer. Ticket creates none of them: the broker does.
- ECS: the API task shows `HEALTHY` (its container health check calls `/health`).
- `TicketIssued`: the worker logs `message published` for each issued order. `GET /internal/v1/outbox-messages` (from inside the VPC) lists messages not published yet, with the broker's last error; the worker logs each failed attempt and keeps retrying. On the broker they arrive in `notification-service.TicketIssued`, or in `ticketto.unrouted` until rabbitmq-service lists notification-service as their consumer.

The worker has no container health check, the same as event-service's worker: it exits non-zero if a consumer thread dies, so ECS replaces it, and consumers reconnect to RabbitMQ on their own with backoff.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Job fails at once with `id_token_burned_project_path` | The project's `ci_id_token_sub_claim_components` was reset to the path-based default; set it back (see GitLab sign-in) |
| `AssumeRoleWithWebIdentity` / "Not authorized" | The trust policy `sub` doesn't match the project ID or branch (see GitLab sign-in) |
| Variables are empty in a `main` job | `main` isn't protected, or a variable's environment scope isn't `production` |
| `deploy` fails with `the rollout of … failed` or `is still not complete` | Read the deployments, service events and stopped-task reasons printed under that line. The container's own output is in CloudWatch `/ecs/ticket-service` (streams `ticket-service/…` for the API, `worker/…` for the worker) |
| Task stopped with `ResourceInitializationError` | The task has no public IP (the default VPC has no NAT), or the execution role can't read a secret |
| `migrate` fails with `Access denied for user 'ticket_service'` | `create-db-user.sh` hasn't been run, or `db-ticketing` was stopped |
| Worker logs connection timeouts to the broker on 5672 | Ticket's security group isn't in rabbitmq-service's `amqp_allowed_sg_ids` yet |
| Buyer requests through the gateway get 401 `The X-User-Id header is required.` | The API task definition lost `AUTH_MODE=gateway` |
| Worker logs `ACCESS_REFUSED` | It is using a broker login other than `rabibi/service/ticket-service` |
| Outbox messages stay `PENDING` with `ACCESS_REFUSED` | The broker doesn't let `ticket-service` publish `ticket.issued` to `ticket.events` (its `topology/messages.yaml` must list `TicketIssued` with publisher ticket-service), or the worker uses another login |
| `TicketIssued` piles up in `ticketto.unrouted` | Nothing consumes it yet: rabbitmq-service must list notification-service as its consumer, then redeploy |
| Buyers get no ticket email but the message is `PUBLISHED` | notification-service's side: check its logs and its `notification-service.TicketIssued` queue and DLQ |

## Rollback

Run the manual **rollback** job on the latest `main` pipeline: both services go back to their previous task definition revision. To pick a specific older revision, set `ROLLBACK_TASK_DEFINITION_ARN` and `ROLLBACK_WORKER_TASK_DEFINITION_ARN` when running the job. Re-running an older pipeline's `deploy` job also works. Migrations are additive, so an older image runs against the current schema.
