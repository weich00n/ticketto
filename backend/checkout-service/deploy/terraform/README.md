# checkout-service on ECS Fargate (Terraform)

Creates checkout-service's own AWS resources in the team account `708677596839` (us-east-1) and runs the API and the worker in the existing `TicketPlatform` cluster, the same way the other services run there: the default VPC's public subnets, a public IP per task (there is no NAT), no load balancer. The API is reached through the team's shared API Gateway (`api`), like user-service ([`gateway.tf`](gateway.tf)).

**Creates:** the `checkout-service` security group (TCP 3002 from inside the VPC only), one inbound rule from that group on each of the DB (3306), RabbitMQ (5672) and reservation-service (8081) groups, the `checkout-service` ECR repository, the `/ecs/checkout-service` log group, the `checkout-service/stripe` secret (empty placeholder keys), the `checkout-service-execution` role, the `gitlab-checkout-service-deploy` role the GitLab pipeline signs in with, the two task definitions (rendered from `../task-definition.json` and `../worker-task-definition.json`) and, once enabled, the `checkout-service` and `checkout-service-worker` services. For the gateway ([`gateway.tf`](gateway.tf)): the `checkout-service` Cloud Map service in `ticketto.local` (the API registers; the worker doesn't), the `checkout-service` VPC link and its `checkout-service-vpclink` security group, and on the shared API two integrations and three routes: `ANY /api/v1/orders` and `ANY /api/v1/orders/{proxy+}` (Cognito JWT authorizer; the gateway sets `X-User-Sub` from the token and overwrites any the caller sent) and `POST /webhooks/stripe` (no authorizer; checkout verifies `Stripe-Signature`).

**Reads only:** the VPC and its subnets, the `checkout-service/db` secret (you create it in step 1) and `rabibi/service/checkout-service` (checkout's RabbitMQ user, created by rabbitmq-service), and the shared API Gateway `api`, its Cognito authorizer and the `ticketto.local` namespace. Nothing else on the teammates' security groups or the API is touched. The API's `$default` stage auto-deploys, so a route change is live for everyone the moment it's applied: review the plan.

State is in the team's shared S3 bucket `rabibi-tfstate-708677596839` (key `checkout-service/terraform.tfstate`, versioned, with an S3 lock so two applies can't run at once), like the other services. Anyone on the team can run this from their own machine: copy `backend.hcl.example` to `backend.hcl` (git-ignored), set your AWS CLI profile in it, and `terraform init -backend-config=backend.hcl`. Never run it without the backend: Terraform would start from an empty state and try to create everything again.

Every command below is PowerShell on Windows. Run them in **one window**: steps 3 and 5 set variables (`$repo`, `$task`) that later lines reuse.

## 0. Open the folder and check your tools

```powershell
cd C:\Users\User\Desktop\g2-team5\backend\checkout-service\deploy\terraform
aws sts get-caller-identity --profile new-profile-name --region us-east-1
terraform version
docker version
```

- `get-caller-identity` must show account `708677596839` and `user/yinam`. Always pass `--profile new-profile-name`: your `default` profile has old keys that AWS rejects.
- If `terraform` is "not recognized", it isn't on your PATH. Use the full path `C:\Users\User\Downloads\terraform\terraform.exe` everywhere this page says `terraform`.
- If `docker version` shows no `Server:` section, start Docker Desktop and wait until it says it's running.

## 1. Database user and secret

You do this by hand, so the password never enters Terraform state or the chat.

**a. Generate a password** and keep it private:

```powershell
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 32 | % {[char]$_})
```

**b. Create the database and user.** In the AWS console: **RDS → Databases → `db-ticketing` → Connect → CloudShell**, signed in as the master user (its password: "Master credentials ARN" → View in Secrets Manager → Retrieve secret value). Run, with your password in place of `<password>`:

```sql
CREATE DATABASE checkout_service CHARACTER SET utf8mb4;
CREATE USER 'checkout_service'@'%' IDENTIFIED BY '<password>';
GRANT ALL PRIVILEGES ON checkout_service.* TO 'checkout_service'@'%';
```

**c. Store it as the `checkout-service/db` secret.** Back in PowerShell, put your password in the first line. The last line deletes the temp file and the variable:

```powershell
$pw = '<password>'
@{username='checkout_service'; password=$pw} | ConvertTo-Json | Set-Content -Encoding ascii $env:TEMP\checkout-db.json
aws secretsmanager create-secret --name checkout-service/db --secret-string file://$env:TEMP\checkout-db.json --profile new-profile-name --region us-east-1
Remove-Item $env:TEMP\checkout-db.json; Remove-Variable pw
```

It prints the secret's ARN (safe to share; it isn't the password).

## 2. Settings file

**a. Look up reservation-service's current private IP:**

```powershell
$t = aws ecs list-tasks --cluster TicketPlatform --service-name reservation-service --query 'taskArns[0]' --output text --profile new-profile-name --region us-east-1
aws ecs describe-tasks --cluster TicketPlatform --tasks $t --query 'tasks[0].containers[0].networkInterfaces[0].privateIpv4Address' --output text --profile new-profile-name --region us-east-1
```

**b. Create the settings file and open it:**

```powershell
copy terraform.tfvars.example terraform.tfvars
notepad terraform.tfvars
```

Set `reservation_base_url = "http://<that IP>:8081"`, keep `image_tag = "0ff5820"` and `deploy_services = false`, and save.

## 3. Create the infrastructure

```powershell
copy backend.hcl.example backend.hcl
terraform init "-backend-config=backend.hcl"
terraform plan
terraform apply
```

The quotes around `-backend-config=…` matter in Windows PowerShell, which otherwise splits the argument at the dot.

- `init` ends with "Terraform has been successfully initialized!".
- `plan` lists what will be created (about 15 resources, **0 to change, 0 to destroy**).
- `apply` shows the same list and asks you to type `yes`. It creates everything except the two services.

## 4. Push the image

The image `checkout-service:0ff5820` is already built on this PC. Tag it for ECR and push it:

```powershell
$repo = terraform output -raw ecr_repository_url
aws ecr get-login-password --profile new-profile-name --region us-east-1 | docker login --username AWS --password-stdin $repo.Split('/')[0]
docker tag checkout-service:0ff5820 "${repo}:0ff5820"
docker push "${repo}:0ff5820"
```

`docker login` should print "Login Succeeded". If `docker tag` says the image doesn't exist, build it first from the service folder: `docker build --target runtime -t checkout-service:0ff5820 ..\..`

## 5. Run the database migration

A one-off task that runs `alembic upgrade head` with the API task definition:

```powershell
'{"containerOverrides":[{"name":"checkout-service","command":["alembic","upgrade","head"]}]}' | Set-Content -Encoding ascii $env:TEMP\migrate.json
$net = "awsvpcConfiguration={subnets=[$(terraform output -raw subnet_ids)],securityGroups=[$(terraform output -raw security_group_id)],assignPublicIp=ENABLED}"
$task = aws ecs run-task --cluster TicketPlatform --launch-type FARGATE --task-definition (terraform output -raw api_task_definition_arn) --network-configuration $net --overrides file://$env:TEMP\migrate.json --query 'tasks[0].taskArn' --output text --profile new-profile-name --region us-east-1
aws ecs wait tasks-stopped --cluster TicketPlatform --tasks $task --profile new-profile-name --region us-east-1
aws ecs describe-tasks --cluster TicketPlatform --tasks $task --query 'tasks[0].[containers[0].exitCode,stoppedReason]' --output text --profile new-profile-name --region us-east-1
Remove-Item $env:TEMP\migrate.json
```

The `describe-tasks` line prints the exit code first: **`0` means the migration succeeded.** Anything else: see Troubleshooting.

## 6. Start the services

```powershell
notepad terraform.tfvars
```

Change `deploy_services` to `true`, save, then:

```powershell
terraform apply
```

Type `yes`. It creates `checkout-service` (2 tasks, scaled up to 4 on CPU by [`autoscaling.tf`](autoscaling.tf)) and `checkout-service-worker` (1 task), and waits until both are stable (a few minutes).

## 7. Check it

```powershell
aws ecs describe-services --cluster TicketPlatform --services checkout-service checkout-service-worker --query 'services[].[serviceName,runningCount,deployments[0].rolloutState]' --output table --profile new-profile-name --region us-east-1
aws logs tail /ecs/checkout-service --since 10m --profile new-profile-name --region us-east-1
```

Both services should show `1` and `COMPLETED`. In the logs: the API serving `/health` (the container health check calls it every 30 s), and the worker's consumers `consuming` and the outbox relay running.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `terraform apply` fails on `aws_iam_role` with **AccessDenied** | You can't create IAM roles. Ask a teammate to create `checkout-service-execution`: trusted by `ecs-tasks.amazonaws.com`, with the managed policy `AmazonECSTaskExecutionRolePolicy` and an inline policy allowing `secretsmanager:GetSecretValue` on `checkout-service/db*`, `rabibi/service/checkout-service*` and `checkout-service/stripe*`. Add `execution_role_arn = "<its ARN>"` to `terraform.tfvars` and run `terraform apply` again. |
| Any other **AccessDenied** / "not authorized" | Your IAM user lacks that action. Send the message to a teammate with admin access; it names the exact action. |
| `apply` fails with "couldn't find resource" on `data.aws_secretsmanager_secret.db` | Step 1c wasn't done, or the secret has another name. |
| `InvalidClientTokenId` / "security token is invalid" | A command is missing `--profile new-profile-name` (Terraform reads it from `terraform.tfvars`). |
| Migration exit code isn't `0`, or a service rolls back | Read the logs: `aws logs tail /ecs/checkout-service --since 15m --profile new-profile-name --region us-east-1`. `Access denied for user` = the DB user/password from step 1 doesn't match. `CannotPullContainerError` = step 4 not done or the tag doesn't match `image_tag`. `ResourceInitializationError` mentioning secrets = the execution role can't read one of the three secrets. |
| Worker logs `outbox relay cycle failed` / can't connect to RabbitMQ | The RabbitMQ rule from step 3 is missing or the `rabibi` broker is down; ask whoever runs rabbitmq-service. |
| Checkout can't reach reservation-service | Its IP changed (reservation's task restarted). Redo step 2a, update `reservation_base_url`, `terraform apply`. |

## Later

- **Stripe keys.** When you have test keys, put them in `$env:TEMP\stripe.json` (outside the repo) as `{"STRIPE_API_KEY":"sk_test_...","STRIPE_WEBHOOK_SECRET":"whsec_..."}`, then:

  ```powershell
  aws secretsmanager put-secret-value --secret-id checkout-service/stripe --secret-string file://$env:TEMP\stripe.json --profile new-profile-name --region us-east-1
  Remove-Item $env:TEMP\stripe.json
  aws ecs update-service --cluster TicketPlatform --service checkout-service --force-new-deployment --profile new-profile-name --region us-east-1
  aws ecs update-service --cluster TicketPlatform --service checkout-service-worker --force-new-deployment --profile new-profile-name --region us-east-1
  ```

  ECS reads secrets only when a task starts, hence the redeploy. Terraform ignores this secret's value after creating it, so `apply` won't reset it.
- **Cloud Map for reservation-service.** reservation-service is registered in the `ticketto.local` namespace, and the pipeline's `RESERVATION_BASE_URL` uses `http://reservation-service.ticketto.local:8081`, so after the first pipeline deploy the IP no longer matters. You can set `reservation_base_url` to the same name too.
- **Stripe webhook.** The gateway routes `POST /webhooks/stripe` to checkout; `terraform output -raw stripe_webhook_url` prints the URL. In the Stripe dashboard (test mode) → Developers → Webhooks → **Add endpoint**, use that URL and select `payment_intent.succeeded`, `payment_intent.payment_failed` and `charge.refunded`. Stripe then shows the endpoint's signing secret (`whsec_…`): put it in `checkout-service/stripe` as `STRIPE_WEBHOOK_SECRET` together with `STRIPE_API_KEY` (the Stripe keys step above, including the redeploy). Until then every webhook gets `400 BAD_SIGNATURE`.
- **Calling the API.** `terraform output -raw orders_api_url`; send a Cognito **access** token as `Authorization: Bearer …` (the gateway maps `username`, which only access tokens carry). Without a valid token the gateway answers `401` before checkout sees the request.
- **New release: the GitLab pipeline.** Every merge to `main` builds the image, pushes it as `:<commit SHA>`, registers new task definition revisions, runs the migration and rolls both services ([`../README.md`](../README.md) Guide A). Terraform ignores which revision a service runs (`ignore_changes = [task_definition]`), so `terraform apply` doesn't undo a pipeline deploy; it still owns the infrastructure and the task-count limits. The counts themselves belong to Application Auto Scaling ([`autoscaling.tf`](autoscaling.tf)): the API runs between `api_min_count` (default 2) and `api_max_count` (default 4) tasks, adding tasks when average CPU passes `api_cpu_target_percent`; the worker is pinned at `worker_desired_count`. Terraform ignores the services' `desired_count`, so an apply never resets a scaled-out API and is safe during a sale. `image_tag` and `reservation_base_url` now only set the revision Terraform registers itself, which a service starts on only if it is recreated.
- **Switching the pipeline on (one-time).** `terraform apply` creates the `gitlab-checkout-service-deploy` role. Then, in GitLab → checkout-service → **Settings → CI/CD → Variables**, add each entry of `terraform output gitlab_ci_variables` as **Protected** with environment scope **`production`**, and add `DEPLOY_ENABLED` = `true` as Protected with scope **All (`*`)**. This needs the Maintainer role in the project. From the next `main` pipeline on, `build-and-push`, `migrate` and `deploy` run.
- **Releasing by hand** (pipeline off): build with the commit's short SHA, set `image_tag` to it, push (step 4), run `terraform apply -target=aws_ecs_task_definition.this` to register the new revisions and run the migration (step 5). Then point the services at them with `aws ecs update-service --cluster TicketPlatform --service <name> --task-definition <ARN> --profile new-profile-name --region us-east-1`; `terraform apply` no longer rolls them.
- **Stop and start (to save money).** In `terraform.tfvars` set `api_min_count = 0`, `api_max_count = 0` and `worker_desired_count = 0`, then `terraform apply`: the autoscaler takes both services to 0 tasks within a minute or two. Set them back to `2`, `4` and `1` and apply to start again. (With the counts at 0 the `deploy` job of a `main` pipeline would fail, so start the services before merging.)
- **Remove everything:** `terraform destroy` (the ECR repository only once it's empty). It leaves the `checkout-service/db` secret and the database alone.
