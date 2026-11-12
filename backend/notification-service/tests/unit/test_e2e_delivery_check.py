"""The e2e's delivery check: `ALL PASSED` must mean SES delivered each email, not just took it."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "e2e" / "aws_e2e.py"
spec = importlib.util.spec_from_file_location("aws_e2e", SCRIPT)
aws_e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aws_e2e)


def sent(kind="EVENT_CANCELLED", notification_id=1, ses_id="ses-1") -> dict:
    line = {"message": "email sent", "kind": kind, "notification_id": notification_id}
    return {**line, "ses_message_id": ses_id} if ses_id else line


def ses_event(event_type: str, ses_id="ses-1") -> dict:
    return {"detail": {"eventType": event_type, "mail": {"messageId": ses_id}}}


def test_a_kind_with_too_few_sends_is_reported():
    lines = [sent("EVENT_PUBLISHED"), sent("EVENT_PUBLISHED"), sent("TICKET_ISSUED")]
    assert aws_e2e.kinds_not_sent(lines) == {
        "EVENT_CANCELLED": 1,
        "ORDER_REFUNDED": 1,
        "REFUND_FAILED": 1,
        "TICKET_ISSUED": 1,
    }


def test_other_peoples_new_event_emails_dont_make_up_for_a_missing_kind():
    assert "TICKET_ISSUED" in aws_e2e.kinds_not_sent([sent("EVENT_PUBLISHED")] * 13)


def test_every_kind_sent_reports_nothing():
    lines = [sent(kind) for kind, n in aws_e2e.EXPECTED_SENT.items() for _ in range(n)]
    assert aws_e2e.kinds_not_sent(lines) == {}


def test_a_delivered_email_is_no_problem():
    assert aws_e2e.undelivered([sent()], [ses_event("Send"), ses_event("Delivery")]) == []


def test_an_accepted_email_with_no_delivery_event_is_a_problem():
    # This run's cancellation and ticket emails: accepted by SES, then nothing.
    [problem] = aws_e2e.undelivered([sent(notification_id=723)], [ses_event("Send")])
    assert "EVENT_CANCELLED" in problem and "723" in problem and "Send" in problem


def test_a_bounce_is_reported_by_name():
    [problem] = aws_e2e.undelivered([sent()], [ses_event("Send"), ses_event("Bounce")])
    assert "Bounce" in problem


def test_events_for_another_email_dont_count():
    assert aws_e2e.undelivered([sent(ses_id="ses-1")], [ses_event("Delivery", "ses-2")])


def test_a_line_with_no_ses_id_is_a_problem():
    assert aws_e2e.undelivered([sent(ses_id=None)], [ses_event("Delivery")])


def test_problems_never_show_an_address():
    event = ses_event("Bounce")
    event["detail"]["mail"]["destination"] = ["alice@example.com"]
    assert "@" not in "".join(aws_e2e.undelivered([sent()], [event]))
