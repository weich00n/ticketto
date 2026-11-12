# Shared API Gateway connectivity

Deployment verified on 28 October 2026: all 18 public/authenticated smoke checks
passed, including a buyer denied organiser access despite forged identity headers
and an organiser accepted. The disposable Cognito user was deleted. Event, Queue,
Reservation, Ticket, User and Checkout HTTP integrations were reached; no purchase
or RabbitMQ workflow was executed. Frontend remains in demo mode. Both OpenTofu
stacks reported no remaining changes after apply.

This separate OpenTofu stack adds Queue, Reservation and Ticket HTTP routes to
`x0vxfeenrl` in us-east-1. It does not deploy the frontend or change demo mode.
Event routes live in `backend/event-service/deploy/terraform/gateway.tf`.
Existing User, Checkout, Stripe webhook and Cognito authorizer remain under their
existing owners. This stack adopts the existing default stage to enable access logs
at `/apigateway/ticketto` (14-day retention), preserving automatic deployment.
RabbitMQ message contracts are separate from HTTP routing.

## Ownership and deployment

State key: `shared-gateway/terraform.tfstate` in the team state bucket. An internal
ALB has separate listeners for Queue (8000), Reservation (8081), Ticket (3006),
Notification (8082), User (8083, forwarding to its tasks' port 8000), Event (3001) and Checkout (3002).
Only the VPC Link security group can reach those listeners. ECS registers task IPs
in target groups, so replacement tasks require no manual IP updates. The ALB incurs
normal AWS load-balancer charges. Reservation's existing DNS registration remains
unchanged so Checkout can continue using reservation-service.ticketto.local:8081,
User's so Notification can continue using user-service.ticketto.local:8000, and
Event's so Ticket and Notification can continue using event-service.ticketto.local:3001.

```sh
export AWS_PROFILE=ticketto
cp backend.hcl.example backend.hcl
tofu init -backend-config=backend.hcl
tofu fmt -check
tofu validate
tofu plan -out=/private/tmp/ticketto-shared-gateway.tfplan
tofu apply /private/tmp/ticketto-shared-gateway.tfplan
python3 attach_services.py            # review only
python3 attach_services.py --apply    # preserves existing attachments and task settings
```

`attach_services.py` attaches the existing ECS services and sets Ticket's
`AUTH_MODE=gateway` in a new revision of its current task definition. It does not
edit teammate repositories. Original service snapshots are stored with restricted
permissions under `/private/tmp/ticketto-gateway-backup` for the current rollout.

**Service owners must persist these AWS changes in their own repositories before
their next infrastructure apply/deployment:**

- Queue, Reservation, Ticket: add their target group (from `tofu output -json
service_attachments`) to their ECS resource's `load_balancer` block, with the
  listed container name and port and a 90-second health-check grace period.
- Notification: its own stack attaches the service to the target group and keeps its
  routes and integration, so only the listener, target group and security group rules
  are here. Create its security group first: `tofu apply -target=aws_security_group.api`
  in notification-service's `deploy/terraform`.
- User: the same split as Notification. Its own stack attaches the service and keeps
  its routes and integration. Its security group already exists.
- Event: the same split as User. Its own stack attaches the service, keeps its routes
  and integrations on the shared VPC link, and no longer has a VPC link of its own.
- Checkout: the same split as Event. Nothing called it through Cloud Map, so it no
  longer registers there either.
- Ticket: add `AUTH_MODE=gateway` to its API task-definition template. A later
  pipeline must not restore `AUTH_MODE=none`. Worker configuration is unchanged.
- Queue: public port-8000 ingress was removed after verifying the private path.
  Remove `0.0.0.0/0` from the owner's `api_allowed_cidrs` configuration so the next
  apply does not recreate it. Keep the ALB ingress rule created here; do not create
  a duplicate rule in another state.
- Broker owner: preserve the new Event-specific AMQP ingress rule managed by the
  Event stack. Event now uses the dedicated `event-service` security group.

Do not import entire teammate ECS services into this state: their existing stacks
remain authoritative. These handoffs avoid two states owning the same ECS resource.

## HTTP authentication

Only queue lobby and availability reads are public in this stack. All buyer and
ticket check-in routes use the existing Cognito JWT authorizer. Verified `sub`,
`username`, and the custom comma-separated `groups` claim overwrite identity
headers. Public integrations remove identity headers. Internal and health routes
are not exposed. `overwrite:path = $request.path` avoids stage prefixes.

The frontend uses a same-origin server proxy; no cross-origin browser request to
Gateway is needed. Later, set these GitLab deployment variables and deploy frontend:

```dotenv
API_GATEWAY_URL=https://x0vxfeenrl.execute-api.us-east-1.amazonaws.com
USE_DEMO_DATA=false
STRIPE_PUBLISHABLE_KEY=<matching Stripe publishable key>
```

Keep demo mode enabled until authenticated purchase, webhook, ticket issuance and
cancellation/refund flows have been tested. Gateway connectivity alone does not
prove payment or RabbitMQ delivery. Never publish or cancel a real event as a smoke test.

## Contract notes for the group (28 October 2026)

The supplied final message document contains inconsistencies to reconcile without
changing unrelated services here:

- EventSoldOut is named `ReservationExpiredEventSoldOut` in one envelope, and its
  routing key is written `event.sold.out`; the current broker uses `event.sold_out`
  and the consumer expects message type `EventSoldOut`.
- Ticket's OrderConfirmed example adds a top-level correlation_id and repeats
  confirmed_at. The broker's schema specifies six envelope fields; correlation IDs
  currently travel as AMQP properties. Use one confirmed_at value.
- Queue's example includes sale_ends_at, but Event's published contract omits it.
  Queue's current parser accepts omission, meaning no configured sale-end deadline.

No notification HTTP routes are defined by the supplied message-only contract.

## Smoke verification

```sh
AWS_PROFILE=ticketto python3 smoke_test.py
AWS_PROFILE=ticketto python3 smoke_test.py --authenticated
```

The authenticated variant creates a disposable Cognito account with invitation
emails suppressed, checks buyer and organiser authorization, then deletes it in
`finally`. No profile, event, reservation, order or payment is created. Credentials
and tokens remain in process memory and are not printed. Wait until ECS rollouts
and ALB target health have settled before testing.
