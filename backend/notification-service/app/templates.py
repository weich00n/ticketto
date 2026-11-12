"""Subject, plain-text and HTML body for each kind of email.

The context was captured when the email was queued (see handlers.py). Every value that goes
into HTML is escaped; subjects and plain text are not HTML. Times are shown in Singapore
time (UTC+8), where the platform's events are.
"""

import html
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import segno

from .models import (
    KIND_EVENT_CANCELLED,
    KIND_EVENT_PUBLISHED,
    KIND_ORDER_REFUNDED,
    KIND_REFUND_FAILED,
    KIND_TICKET_ISSUED,
)

SGT = timedelta(hours=8)

# checkout-service's refund reasons (app/service.py REASON_*), in words.
REFUND_REASONS = {
    "event_cancelled": "the event was cancelled",
    "refund_required": "your booking could not be completed",
    "confirm_rejected": "we could not confirm your seats",
    "late_payment": "your payment arrived after your seats were released",
    "expired_payment": "your payment arrived after your seats were released",
}

_BODY_STYLE = "font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:auto;color:#111"
_FOOTER_STYLE = "font-size:12px;color:#666;margin-top:24px"

# The ticket email looks like the ticket on the site (frontend ticket-view.tsx, globals.css
# .digital-ticket). Email clients ignore flexbox, classes and SVG, so it's tables and inline
# styles, and Gmail ignores web fonts, so it falls back to the site's own Georgia.
_SERIF = "'Cormorant Garamond',Georgia,serif"
_SANS = "'DM Sans',Arial,Helvetica,sans-serif"
_PAPER, _CARD, _INK, _MUTED = "#f9f5eb", "#fffbf1", "#29271f", "#706859"
_TICKET_HEADING = "An evening to remember awaits."
_TICKET_NOTE = (
    "Arrive 45 minutes early. Present your ticket and photo ID at the door. "
    "Each ticket admits one guest."
)
_VIEW_NOTE = "You can also view your tickets any time under My Tickets."
_ICON_CID = "ticket-icon"
_ICON_PNG = Path(__file__).with_name("assets") / "ticket-icon.png"
_TABLE = 'role="presentation" width="100%" cellpadding="0" cellspacing="0"'
_OUTER_FRAME = (
    f"background:{_CARD};border:1px solid #b99a63;border-radius:5px;"
    "border-collapse:separate;margin:0 0 24px"
)
_INNER_FRAME = "border:1px solid #d5c29e;border-radius:3px;border-collapse:separate"
_EYEBROW = "font-size:13px;font-weight:600;letter-spacing:1.8px;color:#87724e"
_TITLE = (
    f"font-family:{_SERIF};font-size:40px;line-height:1.1;font-weight:500;"
    f"color:{_INK};margin:22px 0 15px"
)
_DETAILS_ROW = "padding:24px 26px;border-top:1px dashed #ccb992;border-bottom:1px dashed #ccb992"
_DETAIL_LABEL = f"font-size:13px;letter-spacing:1px;color:{_MUTED};margin-bottom:8px"
_QR_BOX = "background:#ffffff;border:1px solid #e9e5dd"
_TICKET_ID = (
    f"font-family:{_SANS};font-size:9px;letter-spacing:2px;color:{_MUTED};margin:12px 0 25px"
)
_NOTE_HEADING = (
    f"font-family:{_SERIF};font-size:24px;font-weight:500;color:{_INK};margin-bottom:10px"
)
_NOTE = f"font-family:{_SANS};font-size:14px;line-height:1.6;color:{_MUTED}"


@dataclass(frozen=True)
class InlineImage:
    """An image the HTML body shows with <img src="cid:<content_id>">. Gmail blocks data: URIs."""

    content_id: str
    filename: str
    png: bytes


def _sgt(value: str | None) -> str:
    if not value:
        return ""
    when = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None) + SGT
    return when.strftime("%a %d %b %Y, %I:%M %p SGT")


def _page(heading: str, paragraphs: list[str], footer: str = "", cards: str = "") -> str:
    body = "".join(f'<p style="color:#333">{p}</p>' for p in paragraphs) + cards
    foot = f'<p style="{_FOOTER_STYLE}">{footer}</p>' if footer else ""
    return f"""<!doctype html>
<html><body style="{_BODY_STYLE}">
  <p style="letter-spacing:2px;font-size:12px;color:#666">TICKETTO</p>
  <h1 style="font-size:22px;margin:4px 0">{heading}</h1>
  {body}{foot}
</body></html>"""


def _event_published(c: dict) -> tuple[str, str, str]:
    title, name = c.get("title") or "A new event", c.get("name") or "there"
    when, on_sale = _sgt(c.get("starts_at")), _sgt(c.get("release_at"))
    queue_opens = _sgt(c.get("queue_join_opens_at"))  # with its date: it can be the night before
    sale_line = f"Tickets go on sale {on_sale}" if on_sale else ""
    if sale_line and queue_opens:
        sale_line += f" (the queue opens {queue_opens})"
    opt_out = (
        "You're getting this because new-event emails are on for your account. "
        "To turn these emails off, open your profile in Ticketto."
    )
    lines = [
        f"Hi {name},",
        "",
        f"A new event is on Ticketto: {title}.",
        *(x for x in (when and f"When: {when}", sale_line and f"{sale_line}.") if x),
        "",
        opt_out,
    ]
    e = html.escape
    paragraphs = [f"Hi {e(name)},", f"A new event is on Ticketto: <b>{e(title)}</b>."]
    paragraphs += [e(x) for x in (when and f"When: {when}", sale_line and f"{sale_line}.") if x]
    return f"New on Ticketto: {title}", "\n".join(lines), _page(e(title), paragraphs, e(opt_out))


def _event_cancelled(c: dict) -> tuple[str, str, str]:
    title, name = c.get("title") or "your event", c.get("name") or "there"
    when, reason = _sgt(c.get("starts_at")), c.get("reason")
    headline = f"{title}{f' on {when}' if when else ''} has been cancelled by the organiser."
    refund = (
        "Your payment will be refunded automatically to the card you paid with. "
        "We'll email you again when the refund goes through."
    )
    lines = [f"Hi {name},", "", headline]
    if reason:
        lines.append(f"Reason: {reason}")
    lines += ["", refund, "", "Your tickets for this event are no longer valid."]
    e = html.escape
    paragraphs = [f"Hi {e(name)},", e(headline)]
    if reason:
        paragraphs.append(f"Reason: {e(reason)}")
    paragraphs += [e(refund), "Your tickets for this event are no longer valid."]
    return f"Cancelled: {title}", "\n".join(lines), _page(f"Cancelled: {e(title)}", paragraphs)


def _order_refunded(c: dict) -> tuple[str, str, str]:
    title, name = c.get("title") or "your event", c.get("name") or "there"
    amount, order_id = c.get("amount") or "your payment", c.get("order_id") or ""
    why = REFUND_REASONS.get(c.get("reason") or "")
    refunded = f"We've refunded {amount} for {title}" + (f" because {why}." if why else ".")
    timing = "It can take 5 to 10 business days to appear on your statement."
    lines = [f"Hi {name},", "", refunded, timing]
    if order_id:
        lines += ["", f"Order: {order_id}"]
    e = html.escape
    paragraphs = [f"Hi {e(name)},", e(refunded), e(timing)]
    if order_id:
        paragraphs.append(f"Order: {e(order_id)}")
    subject = f"Your refund of {amount} for {title}"
    return subject, "\n".join(lines), _page(f"Refund of {e(amount)}", paragraphs)


def _refund_failed(c: dict) -> tuple[str, str, str]:
    title, name = c.get("title") or "your event", c.get("name") or "there"
    amount, order_id = c.get("amount") or "your payment", c.get("order_id") or ""
    failed = (
        f"We tried to refund {amount} for {title}, but the refund didn't go through, "
        "so you haven't received this money yet."
    )
    ask = "Please reply to this email with your order ID and we'll sort it out."
    lines = [f"Hi {name},", "", failed, ask]
    if order_id:
        lines += ["", f"Order: {order_id}"]
    e = html.escape
    paragraphs = [f"Hi {e(name)},", e(failed), e(ask)]
    if order_id:
        paragraphs.append(f"Order: {e(order_id)}")
    subject = f"We couldn't complete your refund for {title}"
    return subject, "\n".join(lines), _page(e("We couldn't complete your refund"), paragraphs)


def _ticket_cid(ticket: dict) -> str:
    return f"ticket-{ticket['ticket_number']}"


def _ticket_when(value: str | None) -> str:
    """As the site shows it: 20 Nov 2026 · 20:00 SGT."""
    if not value:
        return ""
    when = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None) + SGT
    return f"{when.day} {when:%b %Y} · {when:%H:%M} SGT"


def _seat(ticket: dict, tickets: list[dict]) -> str:
    """As the site shows it: 1 of 2. A republish carries only the tickets still valid, so the
    total can't be lower than the number."""
    total = max(len(tickets), *(t["ticket_number"] for t in tickets))
    return f"{ticket['ticket_number']} of {total}"


def _ticket_heading(c: dict) -> str:
    e = html.escape
    where = [x for x in (_ticket_when(c.get("starts_at")), c.get("venue")) if x]
    return f"""<tr><td align="center" style="padding:26px 26px 24px;font-family:{_SANS}">
        <img src="cid:{_ICON_CID}" alt="" width="27" height="27"
             style="display:block;margin:0 auto 16px">
        <div style="{_EYEBROW}">TICKETTO · YOUR INVITATION</div>
        <div style="{_TITLE}">{e(c.get("title") or "your event")}</div>
        <div style="font-size:15px;line-height:1.6;color:{_MUTED}">
          {"<br>".join(e(x) for x in where)}</div>
      </td></tr>"""


def _ticket_details(c: dict, ticket: dict, tickets: list[dict]) -> str:
    details = [
        ("GUEST", c.get("name")),
        ("CATEGORY", c.get("category_name") or "General admission"),
        ("SEAT", _seat(ticket, tickets)),
    ]
    shown = [(label, value) for label, value in details if value]
    aligns = ["left", *["center"] * (len(shown) - 2), "right"]
    cells = "".join(
        f"""<td align="{align}" valign="top" style="font-family:{_SANS}">
            <div style="{_DETAIL_LABEL}">{label}</div>
            <div style="font-size:16px;font-weight:500;color:{_INK}">{html.escape(value)}</div>
          </td>"""
        for (label, value), align in zip(shown, aligns, strict=True)
    )
    return f"""<tr><td style="{_DETAILS_ROW}">
        <table {_TABLE}><tr>{cells}</tr></table>
      </td></tr>"""


def _ticket_qr(ticket: dict) -> str:
    e = html.escape
    return f"""<tr><td align="center" style="padding:24px 26px 0">
        <table role="presentation" cellpadding="0" cellspacing="0" style="{_QR_BOX}">
          <tr><td style="padding:10px 17px">
            <img src="cid:{e(_ticket_cid(ticket))}" width="155" height="155"
                 alt="QR code for ticket {e(str(ticket["ticket_number"]))}"
                 style="display:block">
          </td></tr>
        </table>
        <div style="{_TICKET_ID}">{e(ticket["ticket_id"])}</div>
      </td></tr>"""


def _ticket_card(c: dict, ticket: dict, tickets: list[dict]) -> str:
    return f"""<table {_TABLE} style="{_OUTER_FRAME}"><tr><td style="padding:7px">
    <table {_TABLE} style="{_INNER_FRAME}">
      {_ticket_heading(c)}
      {_ticket_details(c, ticket, tickets)}
      {_ticket_qr(ticket)}
      <tr><td style="padding:0 26px 26px"><table {_TABLE}><tr>
        <td align="center" style="border-top:1px solid #ded7c7;padding-top:20px">
          <div style="{_NOTE_HEADING}">{_TICKET_HEADING}</div>
          <div style="{_NOTE}">{_TICKET_NOTE}</div>
        </td>
      </tr></table></td></tr>
    </table>
  </td></tr></table>"""


def _ticket_page(c: dict, tickets: list[dict], name: str, confirmed: str) -> str:
    e = html.escape
    cards = "".join(_ticket_card(c, ticket, tickets) for ticket in tickets)
    footer = f"font-size:12px;line-height:1.6;color:{_MUTED};text-align:center;margin:0"
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:{_PAPER}">
<table {_TABLE} style="background:{_PAPER}"><tr><td align="center" style="padding:24px 12px">
  <table {_TABLE} style="max-width:510px"><tr><td style="font-family:{_SANS};color:{_INK}">
  <p style="font-size:15px;margin:0 0 6px">Hi {e(name)},</p>
  <p style="font-size:15px;margin:0 0 20px">{e(confirmed)}</p>
  {cards}
  <p style="{footer}">{e(_VIEW_NOTE)}<br>Order: {e(c.get("order_id") or "")}</p>
  </td></tr></table>
</td></tr></table>
</body></html>"""


def _ticket_issued(c: dict) -> tuple[str, str, str]:
    """Never uses qr_payload: the notifications API renders this after it's removed, and the
    plain-text part shouldn't carry something that works like the ticket."""
    title, name = c.get("title") or "your event", c.get("name") or "there"
    tickets, order_id = c.get("tickets") or [], c.get("order_id") or ""
    category = c.get("category_name") or "General admission"
    confirmed = (
        f"Your ticket for {title} is confirmed."
        if len(tickets) == 1
        else f"Your tickets for {title} are confirmed."
    )
    where = [x for x in (_ticket_when(c.get("starts_at")), c.get("venue")) if x]
    lines = [
        f"Hi {name},",
        "",
        confirmed,
        *where,
        f"Category: {category}",
        "",
        f"Order: {order_id}",
        *(f"Seat {_seat(t, tickets)}: {t['ticket_id']}" for t in tickets),
        "",
        _TICKET_NOTE,
        _VIEW_NOTE,
    ]
    subject = f"Your {len(tickets)} ticket{'' if len(tickets) == 1 else 's'} for {title}"
    return subject, "\n".join(lines), _ticket_page(c, tickets, name, confirmed)


def qr_png(payload: str) -> bytes:
    buffer = io.BytesIO()
    segno.make(payload, error="m", micro=False).save(buffer, kind="png", scale=8, border=4)
    return buffer.getvalue()


def inline_images(context: dict) -> tuple[InlineImage, ...]:
    """The ticket icon, and the QR code of each ticket that still has its qr_payload. Other
    emails have none."""
    codes = tuple(
        InlineImage(_ticket_cid(t), f"{_ticket_cid(t)}.png", qr_png(t["qr_payload"]))
        for t in context.get("tickets") or []
        if t.get("qr_payload")
    )
    if not codes:
        return ()
    return (InlineImage(_ICON_CID, f"{_ICON_CID}.png", _ICON_PNG.read_bytes()), *codes)


_TEMPLATES: dict[str, Callable[[dict], tuple[str, str, str]]] = {
    KIND_EVENT_PUBLISHED: _event_published,
    KIND_EVENT_CANCELLED: _event_cancelled,
    KIND_ORDER_REFUNDED: _order_refunded,
    KIND_REFUND_FAILED: _refund_failed,
    KIND_TICKET_ISSUED: _ticket_issued,
}


def render(kind: str, context: dict) -> tuple[str, str, str]:
    """(subject, text, html) for one email. A subject is always one line."""
    subject, text, html_body = _TEMPLATES[kind](context)
    return " ".join(subject.split()), text, html_body
