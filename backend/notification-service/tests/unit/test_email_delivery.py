# pylint: disable=unbalanced-tuple-unpacking  # [x] = ... asserts exactly one item
"""Sending queued emails: claims, leases, retries and the three email kinds.
Tests 20-27 of the plan."""

import logging
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import boto3
import pytest
from botocore.stub import Stubber
from sqlalchemy import update

from app.email_delivery import (
    CLAIM_LEASE,
    LogSender,
    OutgoingEmail,
    SesSender,
    SmtpSender,
    build_sender,
    claim_due,
    deliver,
    send_due,
    utcnow,
    without_qr_codes,
)
from app.handlers import handle_message
from app.logging_config import JsonFormatter
from app.messages import PoisonMessage, parse
from app.models import (
    KIND_EVENT_CANCELLED,
    KIND_EVENT_PUBLISHED,
    KIND_ORDER_REFUNDED,
    KIND_TICKET_ISSUED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    Notification,
)
from app.templates import InlineImage, qr_png, render
from app.worker import STOP_TIMEOUT_SECONDS
from tests.unit.conftest import (
    EVENT,
    FakeClock,
    envelope,
    notifications,
    sub,
    ticket_context,
    ticket_issued,
)

PUBLISHED_CONTEXT = {
    "name": "Alice",
    "title": "Autumn Live",
    "starts_at": "2026-11-20T12:00:00Z",
    "release_at": "2026-10-20T12:00:00Z",
    "queue_join_opens_at": "2026-10-20T11:45:00Z",
}


def _queue(session_factory, *, kind=KIND_EVENT_PUBLISHED, context=None, due=None, n=1) -> list:
    ids = []
    with session_factory() as session, session.begin():
        for i in range(n):
            row = Notification(
                kind=kind,
                dedupe_key=f"{kind}:{EVENT}:{uuid.uuid4()}",
                source_message_id=str(uuid.uuid4()),
                event_id=EVENT,
                user_id=f"user-{i}",
                recipient=f"u{i}@example.com",
                context=context or PUBLISHED_CONTEXT,
                status=STATUS_PENDING,
                attempts=0,
                next_attempt_at=due or utcnow(),
            )
            session.add(row)
            session.flush()
            ids.append(row.id)
    return ids


def _row(session_factory, notification_id) -> Notification:
    with session_factory() as session:
        return session.get(Notification, notification_id)


def test_due_email_is_sent_and_marked_sent(session_factory, settings, sender):
    [nid] = _queue(session_factory)
    result = send_due(session_factory, settings, sender, deadline=float("inf"))
    assert result == {"sent": 1, "retry": 0, "failed": 0}
    row = _row(session_factory, nid)
    assert row.status == STATUS_SENT and row.sent_at is not None and row.attempts == 1
    assert row.next_attempt_at is None
    [email] = sender.sent
    assert email.to == "u0@example.com"
    assert email.sender == "Ticketto <notifications@ticketto.test>"
    assert email.subject == "New on Ticketto: Autumn Live"


def test_failed_send_backs_off_30_60_120_seconds(session_factory, settings, sender):
    [nid] = _queue(session_factory)
    start = utcnow()
    delays = []
    for attempt in range(1, 4):
        sender.fail_next = 1
        now = start + timedelta(hours=attempt)  # always past the previous retry time
        [claim] = claim_due(session_factory, now, limit=10, max_attempts=5)
        assert claim.attempts == attempt
        assert (
            deliver(
                claim, session_factory=session_factory, settings=settings, sender=sender, now=now
            )
            == "retry"
        )
        row = _row(session_factory, nid)
        assert row.status == STATUS_PENDING and row.last_error == "OSError: smtp down"
        delays.append((row.next_attempt_at - now).total_seconds())
    assert delays == [30, 60, 120]


def test_fifth_failure_is_permanent(session_factory, settings, sender):
    [nid] = _queue(session_factory)
    now = utcnow()
    for _ in range(5):
        sender.fail_next = 1
        now += timedelta(hours=1)
        [claim] = claim_due(session_factory, now, limit=10, max_attempts=5)
        outcome = deliver(
            claim, session_factory=session_factory, settings=settings, sender=sender, now=now
        )
    assert claim.attempts == 5 and outcome == "failed"
    row = _row(session_factory, nid)
    assert row.status == STATUS_FAILED and row.next_attempt_at is None
    assert row.last_error == "OSError: smtp down"
    assert claim_due(session_factory, now + timedelta(days=1), limit=10, max_attempts=5) == []


def test_not_yet_due_and_cancelled_emails_are_not_sent(session_factory, settings, sender):
    _queue(session_factory, due=utcnow() + timedelta(minutes=5))
    [cancelled] = _queue(session_factory)
    with session_factory() as session, session.begin():
        session.execute(
            update(Notification)
            .where(Notification.id == cancelled)
            .values(status=STATUS_CANCELLED, next_attempt_at=None)
        )
    assert send_due(session_factory, settings, sender, deadline=float("inf"))["sent"] == 0
    assert sender.sent == []


def test_claim_is_retried_after_its_lease_expires(session_factory):
    [nid] = _queue(session_factory)
    now = utcnow()
    [claim] = claim_due(session_factory, now, limit=10, max_attempts=5)
    # The run dies here, mid-send: no outcome is ever recorded.
    assert not claim_due(session_factory, now + CLAIM_LEASE - timedelta(seconds=1), 10, 5)
    [again] = claim_due(session_factory, now + CLAIM_LEASE + timedelta(seconds=1), 10, 5)
    assert again.id == claim.id == nid and again.attempts == 2


def test_attempts_used_up_by_crashes_end_as_failed(session_factory):
    [nid] = _queue(session_factory)
    now = utcnow()
    for _ in range(5):  # five runs that each crashed mid-send
        assert claim_due(session_factory, now, 10, 5)
        now += CLAIM_LEASE + timedelta(seconds=1)
    assert not claim_due(session_factory, now, 10, 5)
    assert _row(session_factory, nid).status == STATUS_FAILED


def test_an_abandoned_email_does_not_end_the_run(session_factory, settings, sender):
    now = utcnow()
    [abandoned] = _queue(session_factory, due=now - timedelta(minutes=1))
    with session_factory() as session, session.begin():
        session.execute(update(Notification).where(Notification.id == abandoned).values(attempts=5))
    [next_one] = _queue(session_factory, due=now)

    result = send_due(session_factory, settings, sender, deadline=float("inf"))

    assert result["sent"] == 1
    assert _row(session_factory, abandoned).status == STATUS_FAILED
    assert _row(session_factory, next_one).status == STATUS_SENT


def test_overlapping_runs_never_claim_the_same_email(session_factory):
    _queue(session_factory, n=3)
    now = utcnow()
    first = claim_due(session_factory, now, limit=2, max_attempts=5)
    second = claim_due(session_factory, now, limit=2, max_attempts=5)
    third = claim_due(session_factory, now, limit=2, max_attempts=5)
    claimed = [c.id for c in first + second + third]
    assert len(claimed) == 3 == len(set(claimed))


def test_cancelled_while_sending_stays_cancelled(session_factory, settings, sender):
    [nid] = _queue(session_factory)
    [claim] = claim_due(session_factory, utcnow(), 10, 5)
    with session_factory() as session, session.begin():  # EventCancelled lands mid-send
        session.execute(
            update(Notification).where(Notification.id == nid).values(status=STATUS_CANCELLED)
        )
    assert (
        deliver(claim, session_factory=session_factory, settings=settings, sender=sender)
        == "cancelled"
    )
    assert sender.sent == []
    assert _row(session_factory, nid).status == STATUS_CANCELLED


def test_send_due_stops_at_the_deadline(session_factory, settings, sender):
    _queue(session_factory, n=5)
    clock = FakeClock(start=0, step=1)  # every clock read takes a second
    result = send_due(session_factory, settings, sender, deadline=3, clock=clock)
    assert 0 < result["sent"] < 5
    assert len(notifications(session_factory, status=STATUS_PENDING)) == 5 - result["sent"]


# ---------- the three kinds of email ----------


def test_new_event_email():
    subject, text, html = render(KIND_EVENT_PUBLISHED, PUBLISHED_CONTEXT)
    assert subject == "New on Ticketto: Autumn Live"
    assert "Hi Alice," in text
    assert "Fri 20 Nov 2026, 08:00 PM SGT" in text  # starts_at in Singapore time
    assert "Tue 20 Oct 2026, 08:00 PM SGT" in text  # tickets on sale
    assert "the queue opens Tue 20 Oct 2026, 07:45 PM SGT" in text
    assert "Autumn Live" in html and "<h1" in html
    assert "turn these emails off" in text


def test_a_queue_opening_the_night_before_shows_its_own_date():
    context = {
        **PUBLISHED_CONTEXT,
        "release_at": "2026-10-20T16:15:00Z",  # Wed 00:15 in Singapore
        "queue_join_opens_at": "2026-10-20T15:45:00Z",  # Tue 23:45 in Singapore
    }
    _, text, html = render(KIND_EVENT_PUBLISHED, context)
    assert "on sale Wed 21 Oct 2026, 12:15 AM SGT" in text
    assert "the queue opens Tue 20 Oct 2026, 11:45 PM SGT" in text
    assert "the queue opens Tue 20 Oct 2026, 11:45 PM SGT" in html


@pytest.mark.parametrize(
    ("kind", "context"),
    [
        (KIND_EVENT_PUBLISHED, {}),
        (KIND_EVENT_CANCELLED, {}),
        (KIND_ORDER_REFUNDED, {"amount": "SGD 1.00"}),
    ],
)
def test_a_line_break_in_a_title_never_reaches_the_subject(kind, context):
    subject, _, _ = render(kind, {**context, "title": "Autumn\r\n  Live\nBcc: x@example.com"})
    assert "Autumn Live Bcc: x@example.com" in subject
    assert "\r" not in subject and "\n" not in subject


def test_smtp_accepts_the_subject_of_a_title_with_a_line_break():
    subject, _, _ = render(KIND_EVENT_PUBLISHED, {"title": "Autumn\nLive"})
    EmailMessage()["Subject"] = subject  # raises ValueError on a line break


def test_cancellation_email():
    context = {"name": "Alice", "title": "Autumn Live", "starts_at": None, "reason": "Rain."}
    subject, text, html = render(KIND_EVENT_CANCELLED, context)
    assert subject == "Cancelled: Autumn Live"
    assert "Rain." in text and "Rain." in html
    assert "refund" in text.lower()


def test_refund_email_explains_the_reason_code():
    context = {
        "name": "Alice",
        "title": "Autumn Live",
        "amount": "SGD 88.00",
        "reason": "event_cancelled",
        "order_id": "o-1",
    }
    subject, text, html = render(KIND_ORDER_REFUNDED, context)
    assert subject == "Your refund of SGD 88.00 for Autumn Live"
    assert "the event was cancelled" in text
    assert "o-1" in text and "SGD 88.00" in html


def test_unknown_refund_reason_still_renders():
    context = {
        "name": "A",
        "title": "X",
        "amount": "SGD 1.00",
        "reason": "mystery",
        "order_id": "o",
    }
    _, text, _ = render(KIND_ORDER_REFUNDED, context)
    assert "SGD 1.00" in text


def test_html_is_escaped():
    context = {**PUBLISHED_CONTEXT, "title": "<script>alert(1)</script>", "name": "<b>Eve</b>"}
    subject, _, html = render(KIND_EVENT_PUBLISHED, context)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>Eve</b>" not in html
    assert subject == "New on Ticketto: <script>alert(1)</script>"  # subjects are plain text


class FakeSes:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "ses-1"}


def test_ses_sender_calls_send_email():
    ses = FakeSes()
    SesSender(ses).send(
        OutgoingEmail(
            sender="Ticketto <n@ticketto.test>",
            to="alice@example.com",
            subject="Hi",
            text="plain",
            html="<p>html</p>",
        )
    )
    assert ses.calls == [
        {
            "FromEmailAddress": "Ticketto <n@ticketto.test>",
            "Destination": {"ToAddresses": ["alice@example.com"]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": "Hi", "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": "plain", "Charset": "UTF-8"},
                        "Html": {"Data": "<p>html</p>", "Charset": "UTF-8"},
                    },
                }
            },
        }
    ]


def test_a_send_finishes_before_a_stopping_task_is_killed(settings):
    # Without these, botocore waits 60 s per attempt. A send already started when SIGTERM arrives
    # must still finish, and record its result, before ECS kills the task.
    ses = replace(settings, email_backend="ses", aws_region="us-east-1")
    config = build_sender(ses)._client.meta.config  # pylint: disable=protected-access
    assert config.connect_timeout + config.read_timeout <= STOP_TIMEOUT_SECONDS
    assert config.retries["total_max_attempts"] == 1  # the backoff here does the retrying


def test_utcnow_is_naive_utc_without_microseconds():
    now = utcnow()
    assert now.tzinfo is None and now.microsecond == 0
    assert abs((datetime.now(UTC).replace(tzinfo=None) - now).total_seconds()) < 5


# ---------- ticket emails: inline QR codes (rollout-plan.md Part 2, tests 10-14) ----------


def _deliver_one(session_factory, settings, sender) -> tuple[int, str]:
    [row_id] = _queue(session_factory, kind=KIND_TICKET_ISSUED, context=ticket_context())
    [claim] = claim_due(session_factory, utcnow(), 1, settings.email_max_attempts)
    outcome = deliver(claim, session_factory=session_factory, settings=settings, sender=sender)
    return row_id, outcome


def _stored_qr_codes(session_factory) -> list:
    [row] = notifications(session_factory)
    return [t.get("qr_payload") for t in row.context["tickets"]]


def test_a_ticket_email_has_one_inline_png_per_ticket(session_factory, settings, sender):
    _deliver_one(session_factory, settings, sender)

    [email] = sender.sent
    assert [image.content_id for image in email.images] == [
        "ticket-icon",
        "ticket-1",
        "ticket-2",
    ]
    assert [image.filename for image in email.images] == [
        "ticket-icon.png",
        "ticket-1.png",
        "ticket-2.png",
    ]
    for image in email.images:
        assert image.png.startswith(b"\x89PNG\r\n\x1a\n")
        assert f'src="cid:{image.content_id}"' in email.html
    assert "data:" not in email.html  # Gmail blocks data: images


def test_the_inline_png_encodes_the_qr_payload(session_factory, settings, sender):
    _deliver_one(session_factory, settings, sender)
    [email] = sender.sent
    expected = [t["qr_payload"] for t in ticket_context()["tickets"]]
    assert [image.png for image in email.images[1:]] == [qr_png(payload) for payload in expected]


def test_other_emails_have_no_images(session_factory, settings, sender):
    _queue(session_factory)
    [claim] = claim_due(session_factory, utcnow(), 1, settings.email_max_attempts)
    deliver(claim, session_factory=session_factory, settings=settings, sender=sender)
    assert sender.sent[0].images == ()


def _email_with_images() -> OutgoingEmail:
    return OutgoingEmail(
        sender="Ticketto <n@ticketto.test>",
        to="alice@example.com",
        subject="Hi",
        text="plain",
        html='<img src="cid:ticket-1">',
        images=(InlineImage("ticket-1", "ticket-1.png", b"\x89PNG-bytes"),),
    )


def test_ses_sender_attaches_inline_images():
    ses = FakeSes()
    SesSender(ses).send(_email_with_images())
    [call] = ses.calls
    assert call["Content"]["Simple"]["Attachments"] == [
        {
            "RawContent": b"\x89PNG-bytes",
            "FileName": "ticket-1.png",
            "ContentId": "ticket-1",
            "ContentDisposition": "INLINE",
            "ContentType": "image/png",
            "ContentTransferEncoding": "BASE64",
        }
    ]


def test_the_attachment_is_valid_for_the_installed_ses_client():
    # A stubbed real client checks every parameter against boto3's model of the SES API.
    client = boto3.client(
        "sesv2", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    with Stubber(client) as stub:
        stub.add_response("send_email", {"MessageId": "ses-1"})
        SesSender(client).send(_email_with_images())
        stub.assert_no_pending_responses()


def test_ses_sender_leaves_out_attachments_when_there_are_none():
    ses = FakeSes()
    SesSender(ses).send(replace(_email_with_images(), images=()))
    assert "Attachments" not in ses.calls[0]["Content"]["Simple"]


def test_smtp_sender_sends_images_as_related_parts(monkeypatch, settings):
    sent: list[EmailMessage] = []

    class FakeSmtp:  # pylint: disable=too-few-public-methods
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr("app.email_delivery.smtplib.SMTP", FakeSmtp)
    SmtpSender(settings).send(_email_with_images())

    [message] = sent
    assert "multipart/related" in {part.get_content_type() for part in message.walk()}
    [image] = [p for p in message.walk() if p.get_content_type() == "image/png"]
    assert image["Content-ID"] == "<ticket-1>"
    assert image.get_content_disposition() == "inline"
    assert image.get_filename() == "ticket-1.png"
    assert image.get_content() == b"\x89PNG-bytes"


def test_qr_codes_are_removed_once_the_email_is_sent(session_factory, settings, sender):
    _, outcome = _deliver_one(session_factory, settings, sender)
    assert outcome == "sent"
    assert _stored_qr_codes(session_factory) == [None, None]
    [row] = notifications(session_factory)
    assert [t["ticket_number"] for t in row.context["tickets"]] == [1, 2]  # the rest stays


def test_qr_codes_are_kept_across_retries(session_factory, settings, sender):
    sender.fail_next = 1
    _, outcome = _deliver_one(session_factory, settings, sender)
    assert outcome == "retry"
    assert all(_stored_qr_codes(session_factory))


def test_qr_codes_are_removed_when_the_email_fails_for_good(session_factory, settings, sender):
    sender.fail_next = 1
    last_try = replace(settings, email_max_attempts=1)
    _, outcome = _deliver_one(session_factory, last_try, sender)
    assert outcome == "failed"
    assert _stored_qr_codes(session_factory) == [None, None]


def test_qr_codes_are_removed_when_a_crashed_email_is_abandoned(session_factory, settings):
    _queue(session_factory, kind=KIND_TICKET_ISSUED, context=ticket_context())
    with session_factory() as session, session.begin():
        session.execute(update(Notification).values(attempts=settings.email_max_attempts))

    assert claim_due(session_factory, utcnow(), 1, settings.email_max_attempts) == []

    [row] = notifications(session_factory)
    assert row.status == STATUS_FAILED
    assert _stored_qr_codes(session_factory) == [None, None]


def test_an_email_cancelled_mid_send_stays_cancelled(session_factory, settings):
    class CancelsWhileSending:  # pylint: disable=too-few-public-methods
        def send(self, _email):
            with session_factory() as session, session.begin():
                session.execute(update(Notification).values(status=STATUS_CANCELLED))

    _, outcome = _deliver_one(session_factory, settings, CancelsWhileSending())
    assert outcome == "sent"
    [row] = notifications(session_factory)
    assert row.status == STATUS_CANCELLED


@pytest.mark.parametrize("part", [0, 1, 2], ids=["subject", "text", "html"])
def test_the_qr_text_is_never_in_what_the_email_says(part):
    assert "TKT1." not in render(KIND_TICKET_ISSUED, ticket_context())[part]


def test_the_template_renders_after_the_qr_codes_are_removed():
    scrubbed = without_qr_codes(ticket_context())
    assert render(KIND_TICKET_ISSUED, scrubbed) == render(KIND_TICKET_ISSUED, ticket_context())


def test_removing_qr_codes_leaves_other_contexts_alone():
    assert without_qr_codes(PUBLISHED_CONTEXT) == PUBLISHED_CONTEXT


def test_no_log_line_contains_the_qr_text(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    session_factory, settings, sender, users, events, caplog
):
    caplog.set_level(logging.DEBUG)
    body = ticket_issued(sub(1))
    handle_message(session_factory, users, events, parse(body))
    with pytest.raises(PoisonMessage):
        parse(envelope("TicketIssued", {"tickets": [{"qr_payload": "TKT1.bad"}]}))
    sender.fail_next = 1
    [claim] = claim_due(session_factory, utcnow(), 1, settings.email_max_attempts)
    deliver(claim, session_factory=session_factory, settings=settings, sender=sender)
    with session_factory() as session, session.begin():
        session.execute(update(Notification).values(next_attempt_at=utcnow()))
    send_due(session_factory, settings, LogSender(), time.monotonic() + 5)

    formatter = JsonFormatter()
    lines = [formatter.format(record) for record in caplog.records]
    assert any("email sent" in line for line in lines)  # the logs were captured
    assert not [line for line in lines if "TKT1." in line]


# ---------- tracing an email through SES ----------


class AcceptingSender:
    def __init__(self, message_id):
        self.message_id = message_id

    def send(self, _email):
        return self.message_id


def _sent_line(caplog) -> logging.LogRecord:
    [line] = [r for r in caplog.records if r.getMessage() == "email sent"]
    return line


def test_ses_sender_returns_the_message_id_ses_gave_it():
    assert SesSender(FakeSes()).send(_email_with_images()) == "ses-1"


def test_ses_sender_names_the_configuration_set_when_it_has_one():
    ses = FakeSes()
    SesSender(ses, configuration_set="tracking").send(_email_with_images())
    assert ses.calls[0]["ConfigurationSetName"] == "tracking"


def test_build_sender_passes_on_the_configuration_set(settings):
    tracked = replace(settings, email_backend="ses", ses_configuration_set="tracking")
    assert build_sender(tracked)._configuration_set == "tracking"  # pylint: disable=protected-access


def test_the_sent_line_carries_the_ses_message_id_and_no_address(session_factory, settings, caplog):
    # SES's delivery events are found by this ID, so the line needn't say who it went to.
    caplog.set_level(logging.INFO)
    _queue(session_factory)
    _deliver_one(session_factory, settings, AcceptingSender("ses-42"))
    formatted = JsonFormatter().format(_sent_line(caplog))
    assert '"ses_message_id": "ses-42"' in formatted
    assert "@" not in formatted


def test_a_sender_without_message_ids_logs_none(session_factory, settings, caplog):
    caplog.set_level(logging.INFO)
    _queue(session_factory)
    _deliver_one(session_factory, settings, AcceptingSender(None))
    assert "ses_message_id" not in JsonFormatter().format(_sent_line(caplog))
