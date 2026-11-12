"""End-to-end test of the deployed notification flow in AWS.

Publishes test messages straight to notification-service's and user-service's own queues
(through the broker's default exchange, so teammates' services never see them), and checks
what each message did from the services' "message processed" log lines.
Every run uses fresh event and message IDs, so it can be re-run any time.

It waits for SES's delivery event for every email sent, so it fails if SES accepted an email but
the receiving server didn't. What it can't check is what Gmail then did with it (Spam, say); it
prints which emails to expect.

Usage (from notification-service/, with the AWS CLI credentials for the account):
    .venv/bin/python scripts/e2e/aws_e2e.py --email you@example.com
    .venv/bin/python scripts/e2e/aws_e2e.py --email you@example.com --skip-opt-out

--email is a user with a user-service profile whose address is verified in SES. The opt-out
and inbox steps sign in as that user (password prompted, or TICKETTO_PASSWORD): one switches
new-event emails off and back on, the other checks this run's emails are listed by the
notifications API through the real gateway, then marks them read. It never stops
user-service; the outage test stays manual.
"""

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
CLUSTER = "TicketPlatform"
SERVICES = ("notification-service", "notification-service-worker")
SERVICE_LOGS = "/ecs/notification-service"
USER_SERVICE_LOGS = "/ecs/user-service"
EMAIL_EVENTS_LOGS = "/aws/events/notification-service-emails"
USER_POOL_ID = "us-east-1_2g8EYCEa1"
APP_CLIENT_ID = "2jr04pk573rr5pe95jm0chv47o"
API_URL = "https://x0vxfeenrl.execute-api.us-east-1.amazonaws.com"
BROKER_SECRET = "rabibi/rabbitmq"
BROKER_NAME_TAG = "rabibi"

Q_EP = "notification-service.EventPublished"
Q_EC = "notification-service.EventCancelled"
Q_OR = "notification-service.OrderRefunded"
Q_RF = "notification-service.RefundFailed"
Q_TI = "notification-service.TicketIssued"
Q_US_OC = "user-service.OrderConfirmed"
Q_US_EC = "user-service.EventCancelled"
OUR_QUEUES = (Q_EP, Q_EC, Q_OR, Q_RF, Q_TI, Q_US_OC, Q_US_EC)

LOG_WAIT_SECONDS = 90  # CloudWatch can take a while to show a line
DEAD_LETTER_WAIT_SECONDS = 420  # 5 deliveries, 60 s apart, and some room


def new_id() -> str:
    return str(uuid.uuid4())


def now_z() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Failed(Exception):
    """A check didn't hold."""


# ---------- messages ----------


def envelope(message_type: str, producer: str, payload: dict) -> dict:
    return {
        "message_id": new_id(),
        "message_type": message_type,
        "schema_version": 1,
        "occurred_at": now_z(),
        "producer": producer,
        "payload": payload,
    }


def event_published(event_id: str, title: str) -> dict:
    return envelope(
        "EventPublished",
        "event-service",
        {
            "event_id": event_id,
            "event_version": 1,
            "title": title,
            "venue": "Victoria Concert Hall",
            "starts_at": "2026-12-21T11:30:00Z",
            "release_at": "2026-12-01T04:00:00Z",
            "queue_join_opens_at": "2026-12-01T03:45:00Z",
            "published_at": now_z(),
            "total_capacity": 300,
            "ticket_categories": [
                {
                    "category_id": new_id(),
                    "name": "CAT 1",
                    "price_amount": 8800,
                    "currency": "SGD",
                    "total_quantity": 300,
                }
            ],
        },
    )


def event_cancelled(event_id: str) -> dict:
    return envelope(
        "EventCancelled",
        "event-service",
        {
            "event_id": event_id,
            "event_version": 2,
            "cancellation_id": new_id(),
            "cancelled_at": now_z(),
            "reason": "Venue closure (integration test)",
        },
    )


def order_confirmed(event_id: str, sub: str, email: str) -> dict:
    return envelope(
        "OrderConfirmed",
        "checkout-service",
        {
            "order_id": new_id(),
            "hold_id": new_id(),
            "admission_id": new_id(),
            "user_id": sub,
            "event_id": event_id,
            "category_id": new_id(),
            "quantity": 2,
            "delivery_email": email,
            "total_amount": 10000,
            "currency": "SGD",
            "confirmed_at": now_z(),
        },
    )


def order_refunded(event_id: str, sub: str) -> dict:
    return envelope(
        "OrderRefunded",
        "checkout-service",
        {
            "order_id": new_id(),
            "user_id": sub,
            "event_id": event_id,
            "refund_id": new_id(),
            "amount": 10000,
            "currency": "SGD",
            "reason": "event_cancelled",
        },
    )


def refund_failed(event_id: str, sub: str) -> dict:
    message = order_refunded(event_id, sub)
    message["message_type"] = "RefundFailed"
    message["payload"]["last_error"] = "StripeRejectedError: e2e test"
    return message


def ticket_issued(event_id: str, sub: str, email: str, title: str) -> dict:
    """Made-up QR codes, in ticket-service's format: they can't get anyone in."""
    ticket_ids = [new_id(), new_id()]
    return envelope(
        "TicketIssued",
        "ticket-service",
        {
            "order_id": new_id(),
            "user_id": sub,
            "event_id": event_id,
            "delivery_email": email,
            "title": title,
            "venue": "E2E Hall",
            "starts_at": "2030-01-01T12:00:00Z",
            "category_name": "E2E Category",
            "tickets": [
                {
                    "ticket_id": ticket_id,
                    "ticket_number": number,
                    "qr_payload": f"TKT1.e2e.{ticket_id}.{'e' * 43}",
                }
                for number, ticket_id in enumerate(ticket_ids, start=1)
            ],
        },
    )


def with_new_id(message: dict, **changes) -> dict:
    """A copy with a fresh message_id; `changes` replace top-level or payload fields."""
    copy = json.loads(json.dumps(message))
    copy["message_id"] = new_id()
    for key, value in changes.items():
        if key in copy and key != "payload":
            copy[key] = value
        else:
            copy["payload"][key] = value
    return copy


# ---------- AWS and the broker ----------


class Broker:
    """The RabbitMQ management HTTP API, as the admin user."""

    def __init__(self, base_url: str, password: str):
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"ticketto:{password}".encode()).decode()
        self.auth = f"Basic {token}"

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Authorization": self.auth, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310  # our broker
            return json.loads(response.read() or b"{}")

    def queue(self, name: str) -> dict:
        return self._call("GET", f"/api/queues/%2F/{urllib.parse.quote(name, safe='')}")

    def depth(self, name: str) -> int:
        q = self.queue(name)
        return int(q.get("messages", 0))

    def publish(self, queue: str, message: dict) -> None:
        """To one queue only, through the default exchange."""
        result = self._call(
            "POST",
            "/api/exchanges/%2F/amq.default/publish",
            {
                "routing_key": queue,
                "payload": json.dumps(message),
                "payload_encoding": "string",
                "properties": {
                    "delivery_mode": 2,
                    "content_type": "application/json",
                    "message_id": message["message_id"],
                    "type": message["message_type"],
                    "correlation_id": f"e2e-{message['message_id'][:8]}",
                },
            },
        )
        if not result.get("routed"):
            raise Failed(f"broker did not route the message to {queue}")


@dataclass
class Aws:
    session: boto3.Session = field(default_factory=lambda: boto3.Session(region_name=REGION))

    def __post_init__(self):
        self.ecs = self.session.client("ecs")
        self.logs = self.session.client("logs")

    def running_tasks(self, service: str) -> int:
        found = self.ecs.describe_services(cluster=CLUSTER, services=[service])["services"]
        return found[0]["runningCount"] if found else 0

    def outcomes(self, group: str, message_id: str, since_ms: int) -> list[str]:
        """Outcomes of the service's "message processed" lines for this message, oldest first."""
        lines = self.log_lines(group, f'"{message_id}" "message processed"', since_ms)
        lines.sort(key=lambda line: line.get("timestamp", ""))
        return [line["outcome"] for line in lines if "outcome" in line]

    def log_lines(self, group: str, pattern: str, since_ms: int) -> list[dict]:
        """Every JSON log line matching `pattern` since `since_ms`."""
        lines, token = [], None
        while True:
            kwargs = {"logGroupName": group, "startTime": since_ms, "filterPattern": pattern}
            if token:
                kwargs["nextToken"] = token
            page = self.logs.filter_log_events(**kwargs)
            for event in page["events"]:
                text = event["message"]
                try:
                    lines.append(json.loads(text[text.find("{") :]))
                except ValueError:
                    continue
            token = page.get("nextToken")
            if not token:
                return lines

    def next_outcome(self, group: str, message_id: str, since_ms: int, seen: int) -> str:
        """Wait for this message's outcome number `seen` + 1 (a resend logs a second line)."""
        deadline = time.time() + LOG_WAIT_SECONDS
        while time.time() < deadline:
            found = self.outcomes(group, message_id, since_ms)
            if len(found) > seen:
                return found[seen]
            time.sleep(5)
        raise Failed(f"no new 'message processed' log for {message_id} in {group}")


def broker_url(session: boto3.Session) -> str:
    if os.environ.get("BROKER_UI_URL"):
        return os.environ["BROKER_UI_URL"]
    reservations = session.client("ec2").describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [BROKER_NAME_TAG]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )["Reservations"]
    if not reservations:
        raise Failed("the broker instance isn't running")
    ip = reservations[0]["Instances"][0].get("PublicIpAddress")
    return f"http://{ip}:15672"


def broker_password(session: boto3.Session) -> str:
    secret = session.client("secretsmanager").get_secret_value(SecretId=BROKER_SECRET)
    return json.loads(secret["SecretString"])["password"]


def cognito_sub(session: boto3.Session, email: str) -> str:
    users = session.client("cognito-idp").list_users(
        UserPoolId=USER_POOL_ID, Filter=f'email = "{email}"'
    )["Users"]
    if len(users) != 1:
        raise Failed(f"expected one Cognito user with email {email}, found {len(users)}")
    return next(a["Value"] for a in users[0]["Attributes"] if a["Name"] == "sub")


class UserApi:
    """user-service through API Gateway, signed in as the test user."""

    def __init__(self, session: boto3.Session, email: str, password: str):
        cognito = session.client("cognito-idp")
        # The app client has a secret, so Cognito wants proof of it with every sign-in.
        client = cognito.describe_user_pool_client(UserPoolId=USER_POOL_ID, ClientId=APP_CLIENT_ID)
        digest = hmac.new(
            client["UserPoolClient"]["ClientSecret"].encode(),
            f"{email}{APP_CLIENT_ID}".encode(),
            hashlib.sha256,
        ).digest()
        try:
            auth = cognito.initiate_auth(
                ClientId=APP_CLIENT_ID,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={
                    "USERNAME": email,
                    "PASSWORD": password,
                    "SECRET_HASH": base64.b64encode(digest).decode(),
                },
            )
        except ClientError as error:  # e.g. a wrong password: the other scenarios still run
            raise Failed(f"sign-in failed: {error}") from error
        if "AuthenticationResult" not in auth:
            raise Failed(f"sign-in needs a challenge ({auth.get('ChallengeName')})")
        self.token = auth["AuthenticationResult"]["AccessToken"]

    def _call(self, method: str, body: dict | None = None, path: str = "/api/v1/users/me"):
        request = urllib.request.Request(
            f"{API_URL}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            raise Failed(f"{method} {path}: {error.code} {error.read()[:300]!r}") from error

    def me(self) -> dict:
        return self._call("GET")

    def set_notify(self, on: bool) -> None:
        self._call("PATCH", {"notify_new_events": on})

    def notifications(self, page_size: int = 50) -> dict:
        return self._call("GET", path=f"/api/v1/notifications?page_size={page_size}")

    def unread_count(self) -> int:
        return self._call("GET", path="/api/v1/notifications/unread-count")["unread"]

    def read_all(self) -> None:
        self._call("POST", path="/api/v1/notifications/read-all")


# ---------- the run ----------


@dataclass
class Ctx:  # pylint: disable=too-many-instance-attributes  # one run's shared state
    broker: Broker
    aws: Aws
    sub: str
    email: str
    opted_in: int = 0  # how many users get new-event emails, learnt in scenario 2
    titles: dict[str, str] = field(default_factory=dict)  # event letter -> title
    emails: list[str] = field(default_factory=list)  # what should arrive in the inbox
    # Log searches start here (a minute early, for clock skew between laptop and AWS).
    started_ms: int = field(default_factory=lambda: int(time.time() * 1000) - 60_000)

    def _deliver(self, group: str, queue: str, message: dict) -> str:
        seen = len(self.aws.outcomes(group, message["message_id"], self.started_ms))
        self.broker.publish(queue, message)
        return self.aws.next_outcome(group, message["message_id"], self.started_ms, seen)

    def to_notification(self, queue: str, message: dict) -> str:
        return self._deliver(SERVICE_LOGS, queue, message)

    def to_user_service(self, queue: str, message: dict) -> str:
        return self._deliver(USER_SERVICE_LOGS, queue, message)


def expect(actual: str, expected: str | Callable[[str], bool], what: str) -> None:
    ok = expected(actual) if callable(expected) else actual == expected
    if not ok:
        raise Failed(f"{what}: got {actual!r}, expected {getattr(expected, '__doc__', expected)!r}")
    print(f"      ok  {what}: {actual}")


def queued(n: int) -> str:
    return f"queued_{n}"


def scenario_happy_path(c: Ctx) -> None:
    a = new_id()
    title = f"E2E Symphony {a[:8]}"
    c.titles["A"] = title
    print(f"    event A = {a}")

    oc = order_confirmed(a, c.sub, c.email)
    expect(c.to_user_service(Q_US_OC, oc), "purchase_recorded", "1 OrderConfirmed (user-service)")

    ep = event_published(a, title)
    ep["payload"]["event_version"] = 2
    outcome = c.to_notification(Q_EP, ep)

    def announced(o: str) -> bool:
        """queued_<n>, n >= 1"""
        return o.startswith("queued_") and int(o.split("_")[1]) >= 1

    expect(outcome, announced, "2 EventPublished: new-event emails queued")
    c.opted_in = int(outcome.split("_")[1])
    c.emails.append(f"new event: {title}")
    if c.opted_in > 1:
        print(f"      note {c.opted_in} users are opted in; SES rejects unverified addresses in")
        print("           the sandbox, so theirs show up as retries and then FAILED. Harmless.")

    expect(c.to_notification(Q_EP, ep), "duplicate_message", "3a same message again")
    expect(c.to_notification(Q_EP, with_new_id(ep)), "already_announced", "3b same event, new id")

    ec = event_cancelled(a)
    expect(
        c.to_user_service(Q_US_EC, ec), "cancellation_recorded", "4 EventCancelled (user-service)"
    )
    expect(c.to_notification(Q_EC, ec), queued(1), "4 EventCancelled: one buyer emailed")
    c.emails.append(f"cancelled: {title} (Reason: Venue closure (integration test))")

    expect(c.to_notification(Q_EC, with_new_id(ec)), queued(0), "5 cancelled again: no new email")

    refund = order_refunded(a, c.sub)
    expect(c.to_notification(Q_OR, refund), queued(1), "6 OrderRefunded: buyer emailed")
    c.emails.append(f"refund: SGD 100.00 for {title}")
    expect(
        c.to_notification(Q_OR, with_new_id(refund)), queued(0), "6b same refund_id again: no email"
    )

    failed = refund_failed(a, c.sub)
    expect(c.to_notification(Q_RF, failed), queued(1), "6c RefundFailed: buyer emailed")
    c.emails.append(f"refund failed: SGD 100.00 for {title}")
    expect(
        c.to_notification(Q_RF, with_new_id(failed)), queued(0), "6d same failure again: no email"
    )


def scenario_not_a_buyer(c: Ctx) -> None:
    b = new_id()
    title = f"E2E Not Bought {b[:8]}"
    c.titles["B"] = title
    print(f"    event B = {b}")
    expect(c.to_notification(Q_EP, event_published(b, title)), queued(c.opted_in), "7 published")
    c.emails.append(f"new event: {title}")
    ec = event_cancelled(b)
    expect(c.to_user_service(Q_US_EC, ec), "cancellation_recorded", "7 cancelled (user-service)")
    expect(c.to_notification(Q_EC, ec), queued(0), "7 cancelled: no buyers, no email")

    # 12: a late purchase of the cancelled event isn't recorded...
    late = order_confirmed(b, c.sub, c.email)
    expect(c.to_user_service(Q_US_OC, late), "event_cancelled", "12 late OrderConfirmed skipped")
    # ...so a repeat cancellation still finds no buyer.
    expect(c.to_notification(Q_EC, with_new_id(ec)), queued(0), "12 re-cancel: still no buyer")


def scenario_tickets(c: Ctx) -> None:
    t = new_id()
    title = f"E2E Tickets {t[:8]}"
    c.titles["T"] = title
    print(f"    event T = {t}")

    ti = ticket_issued(t, c.sub, c.email, title)
    expect(c.to_notification(Q_TI, ti), queued(1), "14 TicketIssued: one email with QR codes")
    c.emails.append(f"tickets: 2 QR codes for {title} (they can't be scanned in)")
    expect(c.to_notification(Q_TI, ti), "duplicate_message", "14b same message again: no email")
    resend = with_new_id(ti)
    expect(c.to_notification(Q_TI, resend), queued(1), "14c republished: sent again")
    c.emails.append(f"tickets: the same order again for {title}")


def scenario_opted_out(c: Ctx, api: UserApi) -> None:
    before = bool(api.me().get("notify_new_events", True))
    api.set_notify(False)
    try:
        e = new_id()
        print(f"    event C = {e}")
        outcome = c.to_notification(Q_EP, event_published(e, f"E2E Opted Out {e[:8]}"))
        expect(outcome, queued(c.opted_in - 1), "8 published while you're opted out")
    finally:
        api.set_notify(before)
        print(f"      (new-event emails set back to {before})")


def scenario_cancel_first(c: Ctx) -> None:
    d = new_id()
    print(f"    event D = {d}")
    expect(c.to_notification(Q_EC, event_cancelled(d)), queued(0), "9 cancelled first")
    ep = event_published(d, f"E2E Cancelled First {d[:8]}")
    expect(c.to_notification(Q_EP, ep), "event_cancelled", "9 then published: nobody emailed")


def dead_lettered(c: Ctx, queue: str, message: dict, what: str) -> None:
    """Poison arrives within seconds. A message that's retried takes about 5 minutes: each failed
    run uses one of its 5 deliveries, then the worker waits WORKER_RETRY_SECONDS (60 s)."""
    dlq = f"{queue}.dlq"
    before = c.broker.depth(dlq)
    c.broker.publish(queue, message)
    deadline = time.time() + DEAD_LETTER_WAIT_SECONDS
    while time.time() < deadline:
        if c.broker.depth(dlq) > before and c.broker.depth(queue) == 0:
            print(f"      ok  {what}: in {dlq}")
            return
        time.sleep(5)
    raise Failed(f"{what}: not in {dlq} after {DEAD_LETTER_WAIT_SECONDS} s")


def scenario_failures(c: Ctx) -> None:
    e = new_id()
    bad_version = with_new_id(event_published(e, "E2E Poison"), schema_version=2)
    dead_lettered(c, Q_EP, bad_version, "10a schema_version 2 is poison")
    bad_amount = with_new_id(order_refunded(e, c.sub), amount="100")
    dead_lettered(c, Q_OR, bad_amount, "10b amount as a string is poison")
    unknown = order_refunded(e, "00000000-0000-0000-0000-000000000000")
    dead_lettered(c, Q_OR, unknown, "11 unknown buyer: retried, then dead-lettered")


def scenario_inbox(c: Ctx, api: UserApi) -> None:
    """13: the notifications API (through the real gateway) lists this run's emails to you."""
    listed = api.notifications()
    titles = {item["title"] for item in listed["items"]}
    if "TKT1." in json.dumps(listed):
        raise Failed("the notifications API returned a QR code")
    wanted = {
        f"New on Ticketto: {c.titles['A']}",
        f"Cancelled: {c.titles['A']}",
        f"Your refund of SGD 100.00 for {c.titles['A']}",
        f"We couldn't complete your refund for {c.titles['A']}",
        f"New on Ticketto: {c.titles['B']}",
        f"Your 2 tickets for {c.titles['T']}",
    }
    missing = wanted - titles
    if missing:
        raise Failed(f"not in your notifications: {sorted(missing)}")
    print(f"      ok  13 your notifications list this run's {len(wanted)} emails")
    if api.unread_count() < 1:
        raise Failed("unread-count is 0, but this run's notifications are unread")
    api.read_all()
    if api.unread_count() != 0:
        raise Failed("read-all left notifications unread")
    print("      ok  13 read-all clears the unread count")


DELIVERY_WAIT_SECONDS = 150  # covers the first retry (30 s) and log delay
SES_EVENT_WAIT_SECONDS = 300  # SES usually reports within a minute; it can retry for hours

# The least this run sends, per kind. New-event emails also go to other verified addresses, so a
# plain count could pass with a whole kind missing.
EXPECTED_SENT = {
    "EVENT_PUBLISHED": 2,
    "EVENT_CANCELLED": 1,
    "ORDER_REFUNDED": 1,
    "REFUND_FAILED": 1,
    "TICKET_ISSUED": 2,
}


def kinds_not_sent(lines: list[dict]) -> dict[str, int]:
    """How many more of each kind should have been sent, from the "email sent" lines."""
    found = [line.get("kind") for line in lines]
    short = {kind: wanted - found.count(kind) for kind, wanted in EXPECTED_SENT.items()}
    return {kind: missing for kind, missing in short.items() if missing > 0}


def undelivered(lines: list[dict], events: list[dict]) -> list[str]:
    """One line for each sent email with no SES Delivery event. Events hold addresses: not shown."""
    seen: dict[str, list[str]] = {}
    for event in events:
        detail = event.get("detail", {})
        seen.setdefault(detail.get("mail", {}).get("messageId"), []).append(detail.get("eventType"))
    problems = []
    for line in lines:
        ses_id = line.get("ses_message_id")
        types = seen.get(ses_id, [])
        if "Delivery" not in types:
            what = ", ".join(types) or "no event"
            problems.append(f"{line.get('kind')} #{line.get('notification_id')} {ses_id}: {what}")
    return problems


def check_delivery(c: Ctx) -> None:
    """SES must accept the emails queued, and the receiving servers must accept them from SES."""
    deadline = time.time() + DELIVERY_WAIT_SECONDS
    missing: dict[str, int] = dict(EXPECTED_SENT)
    while missing and time.time() < deadline:
        missing = kinds_not_sent(c.aws.log_lines(SERVICE_LOGS, '"email sent"', c.started_ms))
        if missing:
            time.sleep(10)
    if missing:
        errors = {
            line.get("error", "?")[:300]
            for line in c.aws.log_lines(SERVICE_LOGS, '"email failed"', c.started_ms)
        }
        detail = "".join(f"\n           {e}" for e in errors) or " (no send errors logged)"
        raise Failed(f"not sent: {missing}. SES said:{detail}")

    lines = c.aws.log_lines(SERVICE_LOGS, '"email sent"', c.started_ms)
    print(f"      ok  SES accepted {len(lines)} emails")
    deadline = time.time() + SES_EVENT_WAIT_SECONDS
    while True:
        events = c.aws.log_lines(EMAIL_EVENTS_LOGS, '"eventType"', c.started_ms)
        problems = undelivered(lines, events)
        if not problems:
            print(f"      ok  SES delivered all {len(lines)}")
            return
        if time.time() >= deadline:
            detail = "".join(f"\n           {p}" for p in problems)
            raise Failed(f"{len(problems)} of {len(lines)} emails not delivered:{detail}")
        time.sleep(15)


def preflight(c: Ctx) -> None:
    for name in OUR_QUEUES:
        q = c.broker.queue(name)
        if q.get("messages", 0):
            raise Failed(f"{name} has {q['messages']} messages left; empty it first")
    for name in (Q_US_OC, Q_US_EC):
        if not c.broker.queue(name).get("consumers"):
            raise Failed(f"{name} has no consumer: is user-service-worker running?")
    for service in SERVICES:
        if not c.aws.running_tasks(service):
            raise Failed(f"{service} has no running task in {CLUSTER}")
        print(f"      ok  {service} is running")
    groups = c.aws.logs.describe_log_groups(logGroupNamePrefix=EMAIL_EVENTS_LOGS)["logGroups"]
    if not groups:
        raise Failed(f"{EMAIL_EVENTS_LOGS} doesn't exist: apply deploy/terraform first")


SCENARIOS = {
    "happy": scenario_happy_path,
    "not-a-buyer": scenario_not_a_buyer,
    "tickets": scenario_tickets,
    "cancel-first": scenario_cancel_first,
    "failures": scenario_failures,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    parser.add_argument("--email", required=True, help="the test user's (SES-verified) email")
    parser.add_argument(
        "--skip-opt-out",
        action="store_true",
        help="don't sign in: skips the opt-out and notifications-API (inbox) steps",
    )
    args = parser.parse_args()

    aws = Aws()
    broker = Broker(broker_url(aws.session), broker_password(aws.session))
    c = Ctx(broker=broker, aws=aws, sub=cognito_sub(aws.session, args.email), email=args.email)
    print(f"user {args.email} (sub {c.sub}), broker {broker.base_url}")

    try:
        dlq_before = {q: broker.depth(f"{q}.dlq") for q in OUR_QUEUES}
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        print(
            "A queue this test needs isn't on the broker: press deploy-broker in rabbitmq-service."
        )
        return 1
    steps: list[tuple[str, Callable[[], None]]] = [("preflight", lambda: preflight(c))]
    steps += [(name, lambda fn=fn: fn(c)) for name, fn in SCENARIOS.items()]
    steps.insert(4, ("opted-out", lambda: run_opt_out(c, args)))
    steps.append(("delivery", lambda: check_delivery(c)))
    steps.append(("inbox", lambda: run_inbox(c, args)))

    failures = []
    for name, step in steps:
        print(f"\n== {name}")
        try:
            step()
        except Failed as error:
            print(f"    FAIL {error}")
            failures.append(name)
            if name in ("preflight", "happy"):
                break  # later scenarios depend on these

    print("\n== dead-letter queues (new messages since the start)")
    for q, before in dlq_before.items():
        added = broker.depth(f"{q}.dlq") - before
        print(f"    {q}.dlq: +{added}")
    print("    expected: +1 EventPublished, +2 OrderRefunded (the failure scenarios), others +0")
    print("    Purge them in the broker UI when you're done.")

    print(f"\n== check {args.email} (and Spam) for {len(c.emails)} emails:")
    for line in c.emails:
        print(f"    - {line}")

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'ALL PASSED'}")
    return 1 if failures else 0


_SIGNED_IN: dict[str, UserApi] = {}


def signed_in(c: Ctx, email: str) -> UserApi:
    """Sign in once (password prompted, or TICKETTO_PASSWORD) for the steps that need it."""
    if email not in _SIGNED_IN:
        password = os.environ.get("TICKETTO_PASSWORD") or getpass.getpass(f"Password for {email}: ")
        _SIGNED_IN[email] = UserApi(c.aws.session, email, password)
    return _SIGNED_IN[email]


def run_opt_out(c: Ctx, args: argparse.Namespace) -> None:
    if args.skip_opt_out:
        print("    skipped (--skip-opt-out)")
        return
    if not c.opted_in:
        raise Failed("needs the happy path's opted-in count")
    scenario_opted_out(c, signed_in(c, args.email))


def run_inbox(c: Ctx, args: argparse.Namespace) -> None:
    if args.skip_opt_out:
        print("    skipped (--skip-opt-out: no sign-in)")
        return
    if set(c.titles) != {"A", "B", "T"}:
        raise Failed("needs the happy path, not-a-buyer and tickets events")
    scenario_inbox(c, signed_in(c, args.email))


if __name__ == "__main__":
    sys.exit(main())
