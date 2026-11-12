# Ticketto (G2 Team 5)

A ticketing platform for high-demand events: organisers publish events, buyers wait in a
virtual queue, hold seats, pay with Stripe and receive QR tickets by email. `frontend/` and each folder
under `backend/` is a separate service with its own README and tests. This page explains how to run **the whole system on one laptop**.

## Demo video

[![Ticketto demo video](https://img.youtube.com/vi/CjnJaXu0xBc/hqdefault.jpg)](https://youtu.be/CjnJaXu0xBc)

Watch on YouTube: https://youtu.be/CjnJaXu0xBc

## Prerequisites

- **Docker Desktop** (or Docker Engine) with **Compose v2**. Give Docker at least **6 GB of
  memory** (Docker Desktop → Settings → Resources); the stack runs about 25 containers.
- An internet connection for the first build (images, Maven, npm and pip packages).
- **Chrome or Firefox.** The frontend sets `Secure` sign-in cookies, which Chrome and Firefox
  accept on `http://localhost` but Safari does not.
- Free host ports: 3000, 8080, 8025, 15672 and 9229. You can change any of them in `.env`.

Nothing else is needed: no AWS account, no JDK, Python or Node on the host.

## Start

From this folder:

```sh
docker compose up -d --build
```

The first build takes about 5–10 minutes. Later starts take under a minute. When
`docker compose ps` shows the services as `healthy`, open **http://localhost:3000**. The
one-off setup containers (`seed`, `admission-keys`, `cognito-seed`, `queue-migrate`)
should show `Exited (0)`.

| What | URL |
| --- | --- |
| Web app | http://localhost:3000 |
| API (local gateway, same routes as AWS API Gateway) | http://localhost:8080 |
| Emails (Mailpit: ticket and notification emails) | http://localhost:8025 |
| RabbitMQ management (`ticketto` / `ticketto-local`) | http://localhost:15672 |
| cognito-local (Cognito API emulator) | http://localhost:9229 |

To check the whole system works, run the end-to-end smoke test. It starts the stack first if
it isn't running, and waits until every service is healthy:

```sh
docker compose run --rm smoke-test
```

It signs in as both test accounts and goes through the gateway: browsing, queue, admission,
seat hold, ordering, publishing an event, and the resulting email and in-app notification.
It ends with `All checks passed.` Without Stripe keys, the payment step is reported as `SKIP`.
Afterwards it releases its hold and cancels every published `Smoke Test` event, even if a check
failed, so they don't pile up in the event list.

To stop the stack: `docker compose down`. To stop it and erase all data (databases, users
who signed up, broker state), so the next `up` starts fresh: `docker compose down -v`.

## Test accounts

Use these to sign in at http://localhost:3000/login. They are ready as soon as the stack is up,
with no sign-up or email confirmation needed.

| Role | Email | Password | What it can do |
| --- | --- | --- | --- |
| Ticket buyer | `buyer@test.com` | `Passw0rd!` | Join queues, hold seats, order, and view tickets at `/tickets` |
| Organiser | `organiser@test.com` | `Passw0rd!` | Everything a buyer can, plus create, publish and cancel events and check tickets in at `/organizer`. Owns the demo events below |

These accounts exist only on the local stack (`local/cognito/db`), and `docker compose down -v`
restores them. They don't work on the deployed site, which uses the real Cognito.

### Signing up with your own email

You can also sign up with any email address, and the new account is a buyer. **No confirmation
email arrives, in Gmail or in Mailpit**: cognito-local never sends one, it writes the code to its
log instead. To get the code for the confirmation page, run this and use the last one listed:

```sh
docker compose logs --no-log-prefix cognito-local | grep "Code:"
```

Other emails from the app, such as ticket and new-event emails, never reach a real inbox
either. Read them in Mailpit at http://localhost:8025.

### Demo events

The first start loads three published events owned by the organiser account
(`local/mysql/seed/event_service.sql`). Their times are set relative to that first start:

| Event | State after `up` |
| --- | --- |
| Midnight Frequencies Live | On sale now: join the queue and you are admitted straight away |
| Harbourfront Jazz Night | Queue opens about 5 minutes after start-up, tickets 15 minutes later |
| Lion City Comedy Gala | Tickets released in 7 days |

Events the organiser creates in the app follow the real rule: an event can only be published
if its ticket release is at least 15 minutes away.

## What works without keys, and what needs Stripe or AWS

| Feature | Without any keys | Needs |
| --- | --- | --- |
| Sign up, sign in, profile | ✅ (cognito-local) | — |
| Browse events, organiser create / edit / publish / cancel, image upload | ✅ | — |
| Virtual queue, admission, seat hold, live availability | ✅ | — |
| Paying for an order, and everything after it (order confirmed, tickets issued, ticket email, QR code, check-in, refunds) | ❌ ordering stops with *"Payment could not be started"* | **Stripe test-mode keys** |
| New-event and cancellation emails, in-app notifications | ✅ (emails in Mailpit) | — |
| Real email delivery, Cognito-hosted verification emails | uses Mailpit / cognito-local | AWS (SES, Cognito) |
| Deployment, autoscaling, chaos experiments (`backend/chaos-engineering`) | — | AWS account with the team's infrastructure |

### Enabling payments (Stripe test mode)

1. In the Stripe Dashboard (test mode) → Developers → API keys, copy the secret key
   (`sk_test_…`) and publishable key (`pk_test_…`) into `.env` as `STRIPE_API_KEY` and
   `STRIPE_PUBLISHABLE_KEY`.
2. Install the [Stripe CLI](https://docs.stripe.com/stripe-cli) and forward webhooks to the
   local gateway:
   ```sh
   stripe login
   stripe listen --forward-to localhost:8080/webhooks/stripe \
     --events payment_intent.succeeded,payment_intent.payment_failed,charge.refunded
   ```
   Copy the `whsec_…` secret it prints into `.env` as `STRIPE_WEBHOOK_SECRET`. Keep the
   command running.
3. Apply the keys: `docker compose up -d checkout-service checkout-worker frontend`.
4. Pay with test card `4242 4242 4242 4242`, any future expiry date and any CVC. The order
   is confirmed and the tickets appear in the app and in Mailpit.

Use test-mode keys only, and never commit real values in `.env`.

### Emails

You don't need SMTP access or any email account to run this locally. The notification worker
sends to Mailpit, a fake mail server in Docker (`EMAIL_BACKEND: smtp`, `SMTP_HOST: mailpit`).
Mailpit keeps every email and delivers none of them, so nothing reaches a real inbox. Read them
at http://localhost:8025.

| `EMAIL_BACKEND` | Used by | What happens |
| --- | --- | --- |
| `smtp` | Local stack | Emails go to Mailpit |
| `ses` | AWS (the default) | Real emails are sent through Amazon SES with the worker's AWS role, so no SMTP login is needed |
| `log` | Optional | Emails are only logged, not sent |

## How the local stack maps to AWS

| AWS | Local |
| --- | --- |
| API Gateway + Cognito JWT authorizer | `local/gateway/gateway.py`: the same routes and the same `X-User-Sub` / `X-User-Username` / `X-User-Groups` headers, set from the cognito-local access token |
| Cognito user pool | `cognito-local`, seeded from `local/cognito` (pool `local_0h5xM2Lr`) |
| RDS (one database per service) | one MySQL 8.4 container; `local/mysql/init` creates a database and user per service |
| Amazon MQ / EC2 RabbitMQ | the team broker image built from `backend/rabbitmq-service`, with its full topology and per-service users |
| ElastiCache | two Redis containers (Queue, Reservation) |
| SES | Mailpit |
| Notification worker and API (ECS) | the same two containers (`notification-worker`, `notification-api`) |
| Secrets Manager (Queue's admission signing key) | a throwaway key pair generated on the first start (`admission-keys` volume) |

Every service creates its own tables on start (Alembic or Flyway). All passwords in
`docker-compose.yml` and `local/` are local-only values.

## Troubleshooting

- **A port is already in use:** change it in `.env` (for example `FRONTEND_PORT=3001`) and
  run `docker compose up -d` again.
- **A service keeps restarting:** run `docker compose logs <service>`. On a very first start,
  a service can restart once while MySQL finishes initialising; that's expected.
- **Start from a clean slate:** `docker compose down -v && docker compose up -d --build`.
- **Each service's own tests and compose stack** are described in that service's README.

## Team

CS302 G2 Team 5:

- Wei Choon
- Sophia
- Yinam
- Nithish
- Trevor
- Winnie
