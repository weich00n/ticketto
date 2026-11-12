"""Sending queued emails, with retries.

The handlers queue one `notifications` row per email. Each run claims due PENDING rows one
at a time (FOR UPDATE SKIP LOCKED, so two overlapping runs never take the same row), sends
the email and records the outcome. A claim is a lease: the row's next_attempt_at moves
CLAIM_LEASE into the future, so if the run dies mid-send the email is retried once the
lease expires. A failed send waits retry_base * 2^(attempt-1) seconds (30, 60, 120...) and
becomes FAILED after max_attempts. Outcomes are written only while the row is still
PENDING, so an email cancelled mid-send stays CANCELLED.

A ticket email's QR codes work like the tickets, so they stay in its row only until it
reaches a final status (without_qr_codes), and are never logged.
"""

import logging
import smtplib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .correlation import bound_correlation_id
from .models import STATUS_FAILED, STATUS_PENDING, STATUS_SENT, Notification
from .templates import InlineImage, inline_images, render

logger = logging.getLogger(__name__)

# Each email takes at most ~15 s (the SMTP timeout); a run lasts under a minute.
CLAIM_LEASE = timedelta(minutes=10)
# One attempt, then deliver() backs off. A send must end before ECS kills a stopping task
# (worker.STOP_TIMEOUT_SECONDS), or no failure is recorded and only the lease retries it.
SES_CONNECT_TIMEOUT_SECONDS = 3
SES_READ_TIMEOUT_SECONDS = 8


def utcnow() -> datetime:
    """Naive UTC, whole seconds: how every time is stored."""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def without_qr_codes(context: dict[str, Any]) -> dict[str, Any]:
    """The context with each ticket's qr_payload removed, for a row that's finished with it."""
    if "tickets" not in context:
        return context
    tickets = [
        {k: v for k, v in ticket.items() if k != "qr_payload"} for ticket in context["tickets"]
    ]
    return {**context, "tickets": tickets}


@dataclass(frozen=True)
class OutgoingEmail:
    sender: str
    to: str
    subject: str
    text: str
    html: str
    images: tuple[InlineImage, ...] = ()


class EmailSender(Protocol):
    def send(self, email: OutgoingEmail) -> str | None:
        """Raises if not accepted. Returns the provider's message ID, if it has one."""


class SesSender:
    """Amazon SES API v2, called with the worker's task role (no SMTP credentials)."""

    def __init__(self, client: Any, configuration_set: str = ""):
        self._client = client
        self._configuration_set = configuration_set

    def send(self, email: OutgoingEmail) -> str:
        def part(data: str) -> dict:
            return {"Data": data, "Charset": "UTF-8"}

        simple: dict[str, Any] = {
            "Subject": part(email.subject),
            "Body": {"Text": part(email.text), "Html": part(email.html)},
        }
        if email.images:
            simple["Attachments"] = [
                {
                    "RawContent": image.png,
                    "FileName": image.filename,
                    "ContentId": image.content_id,
                    "ContentDisposition": "INLINE",
                    "ContentType": "image/png",
                    "ContentTransferEncoding": "BASE64",
                }
                for image in email.images
            ]
        tracking = (
            {"ConfigurationSetName": self._configuration_set} if self._configuration_set else {}
        )
        response = self._client.send_email(
            FromEmailAddress=email.sender,
            Destination={"ToAddresses": [email.to]},
            Content={"Simple": simple},
            **tracking,
        )
        return response["MessageId"]


class SmtpSender:
    """Local development and the integration tests: Mailpit catches everything."""

    def __init__(self, settings: Settings):
        self._settings = settings

    def send(self, email: OutgoingEmail) -> None:  # Mailpit's IDs aren't needed
        s = self._settings
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = email.subject, email.sender, email.to
        message.set_content(email.text)
        message.add_alternative(email.html, subtype="html")
        html_part = message.get_payload()[1]
        for image in email.images:
            html_part.add_related(
                image.png,
                maintype="image",
                subtype="png",
                cid=f"<{image.content_id}>",
                filename=image.filename,
                disposition="inline",  # a filename alone makes it an attachment
            )
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(message)


class LogSender:
    """Only logs what would be sent."""

    def send(self, email: OutgoingEmail) -> None:
        logger.info("email (log backend, not sent)", extra={"subject": email.subject})


def build_sender(settings: Settings) -> EmailSender:
    if settings.email_backend == "ses":
        # pylint: disable=import-outside-toplevel  # only needed in AWS
        import boto3
        from botocore.config import Config

        config = Config(
            connect_timeout=SES_CONNECT_TIMEOUT_SECONDS,
            read_timeout=SES_READ_TIMEOUT_SECONDS,
            retries={"total_max_attempts": 1},
        )
        client = boto3.client("sesv2", region_name=settings.aws_region, config=config)
        return SesSender(client, settings.ses_configuration_set)
    if settings.email_backend == "smtp":
        return SmtpSender(settings)
    return LogSender()


@dataclass(frozen=True)
class Claim:
    id: int
    attempts: int


def claim_due(
    session_factory: "sessionmaker[Session]", now: datetime, limit: int, max_attempts: int
) -> list[Claim]:
    with session_factory() as session, session.begin():
        claims: list[Claim] = []
        while len(claims) < limit:  # an abandoned row doesn't use up the limit
            rows = session.scalars(
                select(Notification)
                .where(Notification.status == STATUS_PENDING, Notification.next_attempt_at <= now)
                .order_by(Notification.next_attempt_at, Notification.id)
                .limit(limit - len(claims))
                .with_for_update(skip_locked=True)
            ).all()
            if not rows:
                break
            for row in rows:
                if row.attempts >= max_attempts:
                    # Every attempt so far ended without an outcome (the run died mid-send).
                    row.status, row.next_attempt_at = STATUS_FAILED, None
                    row.context = without_qr_codes(row.context)
                    row.last_error = row.last_error or "Attempts used up without a result."
                    logger.error("email abandoned", extra={"notification_id": row.id})
                    continue
                row.attempts += 1
                row.last_attempt_at = now
                row.next_attempt_at = now + CLAIM_LEASE  # the lease; replaced by the outcome
                claims.append(Claim(row.id, row.attempts))
            session.flush()  # so the next look doesn't see these rows as due
        return claims


def _finish(session_factory, notification_id: int, **values) -> int:
    with session_factory() as session, session.begin():
        result = session.execute(
            update(Notification)
            .where(Notification.id == notification_id, Notification.status == STATUS_PENDING)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0


def deliver(
    claim: Claim,
    *,
    session_factory: "sessionmaker[Session]",
    settings: Settings,
    sender: EmailSender,
    now: datetime | None = None,
) -> str:
    with session_factory() as session:
        row = session.get(Notification, claim.id)
    if row is None or row.status != STATUS_PENDING:
        return "cancelled"  # e.g. a new-event email whose event was cancelled meanwhile
    subject, text, html = render(row.kind, row.context)
    try:
        provider_id = sender.send(
            OutgoingEmail(
                settings.email_from, row.recipient, subject, text, html, inline_images(row.context)
            )
        )
    except Exception as error:  # pylint: disable=broad-exception-caught  # retried below
        now = now or utcnow()
        message = f"{type(error).__name__}: {error}"[:1000]
        log = {"notification_id": claim.id, "attempts": claim.attempts, "error": message}
        if claim.attempts >= settings.email_max_attempts:
            _finish(
                session_factory,
                claim.id,
                status=STATUS_FAILED,
                next_attempt_at=None,
                last_error=message,
                context=without_qr_codes(row.context),
            )
            logger.error("email failed permanently", extra=log)
            return "failed"
        delay = timedelta(seconds=settings.email_retry_base_seconds * 2 ** (claim.attempts - 1))
        _finish(session_factory, claim.id, next_attempt_at=now + delay, last_error=message)
        logger.warning("email failed; will retry", extra={**log, "retry_in": delay.seconds})
        return "retry"
    _finish(
        session_factory,
        claim.id,
        status=STATUS_SENT,
        sent_at=now or utcnow(),
        next_attempt_at=None,
        last_error=None,
        context=without_qr_codes(row.context),
    )
    log = {"notification_id": claim.id, "kind": row.kind}
    if provider_id:
        log["ses_message_id"] = provider_id
    logger.info("email sent", extra=log)
    return "sent"


def send_due(
    session_factory: "sessionmaker[Session]",
    settings: Settings,
    sender: EmailSender,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """Send due emails one at a time until none are left or `deadline` (clock time)."""
    counts = {"sent": 0, "retry": 0, "failed": 0}
    while clock() < deadline:
        claims = claim_due(session_factory, utcnow(), 1, settings.email_max_attempts)
        if not claims:
            break
        with bound_correlation_id(None):
            try:
                outcome = deliver(
                    claims[0], session_factory=session_factory, settings=settings, sender=sender
                )
            except Exception:  # pylint: disable=broad-exception-caught  # the lease retries it
                logger.exception("email delivery crashed", extra={"notification_id": claims[0].id})
                continue
        if outcome in counts:
            counts[outcome] += 1
    return counts
