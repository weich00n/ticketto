# Deploying the broker to AWS

The broker runs on its own EC2 instance, **`rabibi`**, in the account's default VPC. The instance
runs one container: this repo's image, pulled from ECR. Everything it needs is defined in
[`terraform/`](terraform/) (OpenTofu) and is separate from the team's `infra/` stack.

```
GitLab pipeline (main)                              AWS us-east-1, default VPC
──────────────────────                              ──────────────────────────
build-image        → GitLab registry (amd64+arm64)
integration-tests  → tests that exact image
publish-to-ecr     → ECR rabbitmq-service:<sha>, :latest          (every merge)
deploy-broker      ── SSM ──► EC2 rabibi (t4g.small, arm64)       (manual button)
  (OIDC role, no keys)          docker: rabbitmq
                                  5672  ◄── only from amqp_allowed_sg_ids / _cidrs
                                  15672 ◄── SSM port forwarding (+ management_allowed_cidrs)
                                /data: separate EBS volume (queues and messages)
                                password: Secrets Manager rabibi/rabbitmq
```

**Manual deployment:** every merge to `main` builds, tests and publishes an image to ECR.
**deploy-broker** is a manual button on that pipeline: press it to release the image. A broker
restart disconnects every service for about 15 s; they reconnect by themselves, and the durable
queues keep every message. Merges that don't need a broker change (docs, contracts only) can be
left undeployed; the next deploy releases everything merged since. `run-rabbitmq.sh` on the instance:
1. pulls the image
2. stops the old container gracefully
3. starts the new one on the same data
4. waits until it's healthy and `ticketto-check` passes
5. rolls back to the previous image if not

**Access:** no SSH, no key pair, and by default no inbound rule from the internet. The instance's
public IP is for outbound calls to ECR, SSM and Secrets Manager (a default VPC has no NAT). Admin
access goes through SSM Session Manager. To also open the management UI on the public IP, set
`management_allowed_cidrs` (section 2).

**Cost:** about US$18/month while running:
- t4g.small ~$12.30
- 8 + 10 GiB gp3 ~$1.50
- public IPv4 ~$3.65
- secret $0.40

A stopped instance costs about $5/month (disks and IP).

## 1. One-time setup

You need:
- AWS CLI credentials for account 708677596839 (`aws sts get-caller-identity`)
- OpenTofu 1.10+
- Maintainer role on this GitLab project

### 1a. State bucket

The state holds the broker password, so it lives in a private S3 bucket and never in git.

```sh
BUCKET=rabibi-tfstate-$(aws sts get-caller-identity --query Account --output text)
aws s3api create-bucket --bucket "$BUCKET" --region us-east-1
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

### 1b. Create the instance and everything around it

```sh
cd deploy/terraform
cp backend.hcl.example backend.hcl            # put in your bucket name
cp terraform.tfvars.example terraform.tfvars  # optional; every value has a default
tofu init -backend-config=backend.hcl
tofu plan    # 17 to add, 0 to change, 0 to destroy
tofu apply
```

This creates:
- the security group (no inbound rules yet)
- the ECR repository `rabbitmq-service`
- the secret `rabibi/rabbitmq` (the admin user, for people)
- one secret per service, `rabibi/service/<service>`, for its own broker user (the list comes
  from `topology/messages.yaml`)
- the instance role and profile `rabibi`
- the data volume `rabibi-data`
- the instance `rabibi`
- the GitLab OIDC provider and the deploy role `rabibi-gitlab-deploy`

The instance boots, installs Docker and mounts `/data`. ECR is still empty, so **no broker starts
yet**; that's expected.

If the account already has an `https://gitlab.com` OIDC provider, set
`create_gitlab_oidc_provider = false` in `terraform.tfvars`.

### 1c. GitLab variables

In GitLab, open **Settings → CI/CD → Variables** for `backend/rabbitmq-service`.

1. Print the values:
   ```sh
   tofu output -json gitlab_ci_variables
   ```
2. Add `AWS_REGION`, `AWS_ROLE_ARN`, `ECR_REPOSITORY_URI` and `BROKER_INSTANCE_ID` as
   **Protected**, environment scope **`production`**. None of them is a secret, so they don't need
   masking.
3. Add **`DEPLOY_ENABLED` = `true`**, Protected, environment scope **All (`*`)**. It's read by
   `rules:`, and environment-scoped variables are not available there. Until it exists, `main`
   pipelines skip the AWS jobs, so this repo can be merged before AWS is ready.

### 1d. First release

1. Run a pipeline on `main`: **Build → Pipelines → Run pipeline**, or merge anything.
   `publish-to-ecr` copies the tested image to ECR.
2. On that pipeline, **deploy-broker** runs after `publish-to-ecr`. It:
   - installs this commit's `run-rabbitmq.sh` on the instance
   - starts the broker
   - waits for it to be healthy
   - checks the topology with `ticketto-check`

   The admin user is created from the secret when the container starts.
3. Check it (section 2).

From then on, every merge publishes an image; press **deploy-broker** on a `main` pipeline to
release it to the broker.

## 2. Checking the broker

```sh
ID=$(cd deploy/terraform && tofu output -raw broker_instance_id)

# A shell on the instance:
aws ssm start-session --target "$ID"
sudo docker exec rabbitmq ticketto-check    # expect "27 catalogue queues present"

# The management UI on your laptop, at http://localhost:15672:
aws ssm start-session --target "$ID" --document-name AWS-StartPortForwardingSession \
  --parameters portNumber=15672,localPortNumber=15672
```

Log in as `ticketto`. The password is in the secret:

```sh
aws secretsmanager get-secret-value --secret-id rabibi/rabbitmq \
  --query SecretString --output text | jq -r .password
```

`aws ssm start-session` needs the
[Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
on your laptop (`brew install --cask session-manager-plugin`).

**Without the tunnel:** set `management_allowed_cidrs` in `terraform.tfvars` and run `tofu apply`.
The UI is then at `http://<public-ip>:15672` for those sources. The public IP changes whenever the
instance is stopped and started; get the current one with
`aws ec2 describe-instances --instance-ids "$ID" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text`.
It's plain HTTP, so the admin password crosses the network unencrypted. Prefer
your own IP as `<ip>/32` (`curl -s https://checkip.amazonaws.com`) over `0.0.0.0/0`, and empty the
list again once services rely on the broker. Add the rule here, not in the console: a console
rule is invisible to OpenTofu and stays open after the list is emptied.

## 3. Connecting services

Nothing can connect at first. When a service is deployed:

1. **Allow it in.** Add its security group to `amqp_allowed_sg_ids` in `terraform.tfvars` (or a
   peered VPC's CIDR to `amqp_allowed_cidrs`), then run `tofu apply`. Only the security group
   rules change.
2. **Give it its own URL.** Each service has its own secret and user, which can only touch that
   service's queues, exchange and routing keys (docs/MESSAGING.md). Get the ARNs with
   `tofu output service_secret_arns`, and give each team only theirs. In the ECS task
   definition:
   ```json
   {"name": "RABBITMQ_URL", "valueFrom": "<that service's secret ARN>:url::"}
   ```
   The service's task execution role needs `secretsmanager:GetSecretValue` on that ARN.
   **Never give a service the admin secret, `rabibi/rabbitmq`.**

Services in the team's `infra/` VPC (`10.0.0.0/16`) can't reach this VPC (`172.31.0.0/16`) until
the two are peered. The ranges don't overlap, so peering is possible.

The private IP survives stopping and starting the instance, and changes only if the instance is
replaced. The `tofu apply` that replaces it also updates the secret's `host` and `url`. Services
pick the new value up when they restart.

## 4. Day to day

| Task | How |
|---|---|
| Release a new version | Merge to `main`. **deploy-broker** runs by itself at the end of the pipeline. |
| Roll back | Retry **deploy-broker** on an older `main` pipeline. A failed deploy rolls back by itself. |
| Look at failed messages | In the UI, open `<queue>.dlq` → **Get messages**. The `x-death` header says which queue, why and when. |
| Replay failed messages | Fix the consumer first. Then on the `.dlq` queue page, **Move messages** to the original queue. This needs the shovel plugin, which the image enables. |
| Deliver held messages | Once `messages.yaml` gives a message a consumer and that is deployed, go to **Admin → Shovel Management → Add a new shovel** and fill it in like this. Each message keeps its own routing key. A shovel publishes to one exchange, so use the exchange of the publisher whose message gained a consumer. Messages from other publishers, and any that still have no consumer, land back in `ticketto.unrouted`; repeat with the next exchange if needed.<br>• Source: queue `ticketto.unrouted`<br>• Destination: the publisher's exchange (e.g. `ticket.events`), routing key blank<br>• Delete after: **queue-length**, so the shovel moves what is there now and then removes itself |
| Remove a queue | Removing it from `messages.yaml` does not delete it from the running broker (imports only add). Drain it, then `sudo docker exec rabbitmq rabbitmqctl delete_queue <name>`. |
| Rotate a password | Admin: `tofu apply -replace=random_password.broker`. A service: `tofu apply -replace='random_password.service["event-service"]'`. Then retry **deploy-broker** on the latest `main` pipeline (the container reads passwords at start-up), and restart the affected services. |
| Add a service user | Add the service to `messages.yaml` (as a publisher or consumer). Run `tofu apply` (it creates `rabibi/service/<service>`) **before** pressing **deploy-broker**. Without its secret the user isn't created, and `ticketto-check` warns about it; retry **deploy-broker** once the secret exists. |
| Save money | `aws ec2 stop-instances --instance-ids "$ID"`, and later `start-instances`. Docker restarts the broker on boot. Services can't connect while it's stopped; their outboxes keep the messages. |
| Change the instance or boot script | Edit `terraform/`, then `tofu apply`. Edits to `boot-rabbitmq.sh` or a new AMI only apply to a new instance: `tofu apply -replace=aws_instance.broker`. `/data` and the last deployed tag survive, so it comes back on the same image. Edits to `run-rabbitmq.sh` need nothing: every deploy installs it. |

**Never `docker kill` the broker, and never `docker rm -f` it while it is running.** A hard kill
within seconds of a queue being created can lose that queue and its messages. On 4.3.6, killing a
broker straight after first boot lost the messages in `ticketto.unrouted` in 4 of 10 runs; a
graceful stop never did (`tests/integration/test_persistence.py`). `run-rabbitmq.sh` always stops
gracefully, and the container's `--stop-timeout 60` makes Docker do the same when the instance
stops.

## 5. Removing it

```sh
cd deploy/terraform
tofu destroy    # stops at the data volume: it has prevent_destroy
```

To delete the data volume and its messages too:
1. Set `prevent_destroy = false` on `aws_ebs_volume.data` in `main.tf`.
2. Set `allow_destroy_data = true`, so the ECR repository can be deleted with its images.
3. Run `tofu apply`, then `tofu destroy` again.

Finally, delete the GitLab variables, or set `DEPLOY_ENABLED` to anything but `true`.

## Relation to the team's `infra/`

`infra/` (never applied as of 2026-10-20) has its own shared broker host in
`modules/data/broker.tf`, which runs RabbitMQ and both Redis instances. If the team applies
`infra/` later:
- **Remove its RabbitMQ parts first,** so there aren't two brokers: the container in
  `broker-user-data.sh.tftpl`, the `broker/rabbitmq` secret, and the 5672 rules.
- **Point its services at this broker:** the `rabbitmq_secret_arn` output and VPC peering.
- **GitLab OIDC provider:** `infra/modules/cicd` creates the account's only `https://gitlab.com`
  provider, and so does this stack. Whichever is applied second must reuse the existing one. Here
  that's `create_gitlab_oidc_provider = false`; in `infra/` it would need a data source.
