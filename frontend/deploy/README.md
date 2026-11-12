# Deploying the frontend to AWS

The GitLab pipeline (`.gitlab-ci.yml`) deploys every commit on `main` to ECS on Fargate. The Next.js server runs from the image built by the [`Dockerfile`](../Dockerfile), behind a public load balancer, with CloudFront in front for HTTPS. The AWS resources are defined in [`terraform/`](terraform/) (OpenTofu), the same way as the broker's.

```text
browser ──HTTPS──▶ CloudFront ──HTTP──▶ ALB ──:3000──▶ ECS task (frontend)
                                                          │
                                                          ├─▶ Cognito (sign-in)
                                                          └─▶ API Gateway (backend services)
```

HTTPS is required: the sign-in cookies are `secure` in production, so browsers drop them over plain HTTP. CloudFront serves the app at **https://ticketo-cs302.com** (and `www.`), with an ACM certificate, and still on its own `*.cloudfront.net` address. OpenTofu owns the certificate, its DNS validation records and the Route 53 A records, which alias the domain to CloudFront. Never point the domain at the ALB: it accepts traffic from CloudFront only.

## Who manages what

| Managed by OpenTofu ([`terraform/`](terraform/))                                         | Managed by the pipeline                                                        |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Security groups, ALB, target group, CloudFront, the domain's certificate and DNS records | The container image in ECR (`:<commit SHA>`, `:latest`)                        |
| ECR repository and log group                                                             | Each task definition revision ([`task-definition.json`](task-definition.json)) |
| Task execution role and the GitLab deploy role                                           | Rolling the ECS service onto the new revision                                  |
| The ECS service (shell only)                                                             |                                                                                |

The pipeline:

1. **build-and-push** builds the `runtime` image and pushes it to ECR as `:<commit SHA>` and `:latest`.
2. **deploy** ([`scripts/ci/ecs-deploy.sh`](../scripts/ci/ecs-deploy.sh)):
   - fills in [`task-definition.json`](task-definition.json) and registers a new revision;
   - points the ECS service at it and waits until it is stable;
   - fails if ECS rolled the deployment back.

GitLab signs in to AWS with **OIDC**: each job gets a short-lived token, and AWS lets it assume an IAM role only for this project's `main` branch. No AWS keys are stored in GitLab.

This page is the one-time setup. The region is `us-east-1` throughout, and the cluster is the team's existing `TicketPlatform`.

---

## 1. Before you start

You need:

- AWS CLI credentials for the team's account (`aws sts get-caller-identity` shows it)
- OpenTofu 1.10 or newer
- Maintainer role on this GitLab project

**State bucket.** The state lives in a private S3 bucket, never in git. Reuse the bucket the broker stack created, `rabibi-tfstate-<ACCOUNT_ID>`, with this project's own key (`frontend/terraform.tfstate`). If it doesn't exist, create one:

```sh
BUCKET=rabibi-tfstate-$(aws sts get-caller-identity --query Account --output text)
aws s3api create-bucket --bucket "$BUCKET" --region us-east-1
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

**GitLab identity provider.** The stack looks up the account's `gitlab.com` provider (IAM → Identity providers). The broker stack creates it; if it isn't there, add it once (OpenID Connect, URL `https://gitlab.com`, audience `https://gitlab.com`).

## 2. The Cognito secret (by hand)

Its value never goes through OpenTofu, so create it in **Secrets Manager** yourself: a secret named `frontend/cognito` with one key, `client_secret`, set to the app client's secret (Cognito → User pools → App clients). The execution role reads exactly that key.

## 3. Create the infrastructure

```sh
cd deploy/terraform
cp backend.hcl.example backend.hcl            # put in your bucket name
cp terraform.tfvars.example terraform.tfvars  # optional; every value has a default
tofu init -backend-config=backend.hcl
tofu plan    # about 18 to add, 0 to change, 0 to destroy
tofu apply
```

This creates:

- the security groups `frontend-alb` and `frontend`
- the ECR repository `frontend` and the log group `/ecs/frontend`
- the roles `frontend-execution` and `gitlab-frontend-deploy`
- the internet-facing load balancer, its target group (health check `/api/health`) and listener
- the CloudFront distribution

If you created any of these by hand first, delete them before the apply, or it fails with "already exists".

The service itself is created later (step 5), because ECS needs a task definition first.

## 4. GitLab CI/CD variables

`tofu output gitlab_variables` prints the values that come from AWS. **GitLab → Settings → CI/CD → Variables**, for each variable:

- tick **Protect variable**, so only `main` sees it;
- set **Environment scope** to `production`, so only the deploy jobs see it;
- leave **Visibility** on Visible (none of these is a password; the secret stays in Secrets Manager).

| Variable                 | Value                                                                   |
| ------------------------ | ----------------------------------------------------------------------- |
| `AWS_REGION`             | from the output                                                         |
| `AWS_ROLE_ARN`           | from the output                                                         |
| `ECR_REPOSITORY_URI`     | from the output                                                         |
| `ECS_CLUSTER`            | from the output                                                         |
| `ECS_SERVICE`            | from the output                                                         |
| `ECS_EXECUTION_ROLE_ARN` | from the output                                                         |
| `COGNITO_SECRET_ARN`     | from the output                                                         |
| `COGNITO_USER_POOL_ID`   | `us-east-1_AbC123xyz`                                                   |
| `COGNITO_CLIENT_ID`      | the app client ID                                                       |
| `API_GATEWAY_URL`        | the invoke URL, no trailing slash. Leave unset until the gateway exists |
| `USE_DEMO_DATA`          | `true` until the backend is reachable, then `false`. Default `true`     |
| `STRIPE_PUBLISHABLE_KEY` | `pk_test_...`. Leave unset to hide card payment                         |

Make `main` a protected branch (it is by default), or protected variables won't reach it.

Finally, add **`DEPLOY_ENABLED`** = `true`: protected, but with environment scope **All (`*`)**, because the pipeline reads it when deciding which jobs to create. Until it is set, `main` pipelines run the checks and build the image but skip `build-and-push` and `deploy`.

To change `API_GATEWAY_URL` or `USE_DEMO_DATA` later, edit the variable and run a new `main` pipeline: the values are written into the task definition on each deploy.

## 5. First deployment (one-off ordering)

A service can't exist without a task definition, and the pipeline registers the task definition. So the very first time:

1. Merge to `main` (or run a new `main` pipeline). `build-and-push` succeeds and `deploy` registers the task definition `frontend:1`, then fails at `UpdateService` with `ServiceNotFoundException`. That's expected.
2. Create the service. In `deploy/terraform/terraform.tfvars` set:
   ```hcl
   create_service = true
   ```
   then run `tofu apply`. The service starts on the revision the pipeline registered, behind the load balancer, with the deployment circuit breaker and rollback on. `ecs-deploy.sh` relies on it to detect bad releases.
3. Run a new `main` pipeline. From then on every merge to `main` deploys on its own.

OpenTofu ignores later task definition revisions and the task count, so applying again never rolls the app back.

## 6. Checking a deployment

- **GitLab:** the pipeline shows `build-and-push` and `deploy` green, and **Operate → Environments → production** lists the deployed commit.
- **ECS:** the service's primary deployment uses the new revision and the task is `RUNNING` and `HEALTHY`.
- **ALB:** the target is healthy.
- **App:** `tofu output app_url` gives the HTTPS address. `<app_url>/api/health` returns `{"status":"ok","service":"frontend"}`, and the sign-in page loads.
- **End to end:** sign up and sign in, then browse events. With `USE_DEMO_DATA=false` the events come from event-service.

## Changing the infrastructure

Edit the files in [`terraform/`](terraform/), then `tofu plan` and `tofu apply`. Don't change these resources in the console: the next apply would undo it.

## Troubleshooting

| Symptom                                                                  | Likely cause                                                                                                                                                                          |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tofu apply` says a resource "already exists"                            | It was created by hand earlier. Delete it, or import it into the state                                                                                                                |
| `tofu plan` can't find the secret or the identity provider               | `frontend/cognito` doesn't exist yet (step 2), or the account has no `gitlab.com` identity provider                                                                                   |
| `build-and-push` fails at `AssumeRoleWithWebIdentity` / "Not authorized" | The project path in the trust policy doesn't match (`gitlab_project_path`) or the job isn't on `main`                                                                                 |
| Variables are empty in a `main` job                                      | `main` isn't protected, or the variable's environment scope isn't `production`                                                                                                        |
| `deploy` fails with `Unfilled placeholders`                              | A required variable is missing or scoped wrong                                                                                                                                        |
| `deploy` fails with `ServiceNotFoundException`                           | The service doesn't exist yet (step 5), or `ECS_SERVICE` or `ECS_CLUSTER` is wrong. Variables are read when a pipeline is created, so run a new pipeline after changing them          |
| `deploy` says the service rolled back                                    | The new task failed its health check. Read the stopped task's reason and the `/ecs/frontend` log. Common causes: the task can't read `frontend/cognito`, or the wrong secret key name |
| Task starts but `/login` returns 500                                     | `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID` or the secret's `client_secret` is wrong                                                                                                  |
| Sign-in appears to work but the next page asks to sign in again          | The page was opened over plain HTTP. Use the CloudFront HTTPS address                                                                                                                 |
| CloudFront shows 502 or 504                                              | The target group has no healthy target, or the service isn't running yet                                                                                                              |
| Events don't load from the backend                                       | `USE_DEMO_DATA` is still `true`, or `API_GATEWAY_URL` is unset or wrong                                                                                                               |
