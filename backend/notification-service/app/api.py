"""The notifications API: a signed-in user's in-app notifications (the frontend's bell).

  GET  /api/v1/notifications?page=&page_size=   newest first, the frontend's Page shape
  GET  /api/v1/notifications/unread-count       {"unread": n}
  POST /api/v1/notifications/{id}/read          204; 404 if it isn't the caller's
  POST /api/v1/notifications/read-all           204

API Gateway checks the Cognito token, then sets X-User-Sub from it, replacing anything the
client sent. The app trusts that header, so nothing may reach it except through the gateway
(deploy/terraform: its security group allows only the VPC link). Every query is filtered by
it, so nobody can see or change someone else's notifications. A notification is "read" once
the user has seen it in the app.

Each item is an email this service queued for the user, except ones withdrawn before
sending (an event cancelled before its announcement went out). Failed emails still show:
the app is a channel of its own.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .correlation import get_correlation_id
from .email_delivery import utcnow
from .models import STATUS_FAILED, STATUS_PENDING, STATUS_SENT, Notification
from .templates import render

VISIBLE = (STATUS_PENDING, STATUS_SENT, STATUS_FAILED)
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
PREVIEW_CHARS = 200
NO_STORE = {"cache-control": "no-store"}

router = APIRouter(prefix="/api/v1/notifications")


class Problem(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def problem_response(problem: Problem) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        headers=NO_STORE,
        content={
            "error": {"code": problem.code, "message": problem.message},
            "correlation_id": get_correlation_id(),
        },
    )


def _caller(request: Request, x_user_sub: Annotated[str | None, Header()] = None) -> str:
    caller = (x_user_sub or "").strip().lower()
    if not caller:
        raise Problem(401, "UNAUTHORIZED", "Sign in to see your notifications.")
    request.state.user_sub = caller
    return caller


Caller = Annotated[str, Depends(_caller)]


def _positive_int(query: dict, name: str, default: int, maximum: int | None = None) -> int:
    raw = query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise Problem(400, "INVALID_REQUEST", f"{name} must be a whole number.") from None
    if value < 1 or (maximum is not None and value > maximum):
        limit = f"between 1 and {maximum}" if maximum else "at least 1"
        raise Problem(400, "INVALID_REQUEST", f"{name} must be {limit}.")
    return value


def _iso(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _preview(text: str) -> str:
    """The email's first line after the greeting ("Hi Sophia,")."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    body = lines[1:] if lines and lines[0].startswith("Hi ") else lines
    first = body[0] if body else ""
    return first if len(first) <= PREVIEW_CHARS else first[: PREVIEW_CHARS - 1] + "…"


def _item(row: Notification) -> dict:
    subject, text, _html = render(row.kind, row.context)
    return {
        "notification_id": row.id,
        "kind": row.kind,
        "title": subject,
        "preview": _preview(text),
        "event_id": row.event_id,
        "order_id": row.context.get("order_id"),
        "created_at": _iso(row.created_at),
        "email_status": row.status,
        "read_at": _iso(row.read_at),
    }


def _mine(caller: str):
    return (Notification.cognito_sub == caller, Notification.status.in_(VISIBLE))


def _list(session: Session, caller: str, query: dict) -> tuple[int, Any]:
    page = _positive_int(query, "page", 1)
    page_size = _positive_int(query, "page_size", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    total = session.scalar(select(func.count()).select_from(Notification).where(*_mine(caller)))
    rows = session.scalars(
        select(Notification)
        .where(*_mine(caller))
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return 200, {
        "items": [_item(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total_items": total or 0,
    }


def _unread_count(session: Session, caller: str) -> tuple[int, Any]:
    unread = session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(*_mine(caller), Notification.read_at.is_(None))
    )
    return 200, {"unread": unread or 0}


def _read_one(session: Session, caller: str, raw_id: str) -> tuple[int, Any]:
    if not raw_id.isdigit():
        raise Problem(400, "INVALID_REQUEST", "The notification ID must be a number.")
    row = session.scalar(select(Notification).where(Notification.id == int(raw_id), *_mine(caller)))
    if row is None:
        raise Problem(404, "NOT_FOUND", "Notification not found.")
    if row.read_at is None:  # the first time it was seen is kept
        row.read_at = utcnow()
    return 204, None


def _read_all(session: Session, caller: str) -> tuple[int, Any]:
    session.execute(
        update(Notification)
        .where(*_mine(caller), Notification.read_at.is_(None))
        .values(read_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    return 204, None


def _json(status: int, body: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content=body, headers=NO_STORE)


def _no_content() -> Response:
    return Response(status_code=204, headers=NO_STORE)


@router.get("")
def list_notifications(request: Request, caller: Caller) -> JSONResponse:
    with request.app.state.session_factory() as session, session.begin():
        return _json(*_list(session, caller, dict(request.query_params)))


@router.get("/unread-count")
def unread_count(request: Request, caller: Caller) -> JSONResponse:
    with request.app.state.session_factory() as session, session.begin():
        return _json(*_unread_count(session, caller))


@router.post("/read-all")
def read_all(request: Request, caller: Caller) -> Response:
    with request.app.state.session_factory() as session, session.begin():
        _read_all(session, caller)
    return _no_content()


@router.post("/{notification_id}/read")
def read_one(request: Request, caller: Caller, notification_id: str) -> Response:
    with request.app.state.session_factory() as session, session.begin():
        _read_one(session, caller, notification_id)
    return _no_content()
