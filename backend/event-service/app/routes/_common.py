from datetime import UTC, datetime
from typing import Any

from flask import current_app, request
from pydantic import BaseModel, ValidationError
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from ..errors import validation_error
from ..models import Event


def open_session() -> Session:
    return current_app.extensions["session_factory"]()


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_body[M: BaseModel](model: type[M]) -> M:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise validation_error("A JSON object request body is required.")
    try:
        return model.model_validate(body)
    except ValidationError as error:
        details: dict[str, str] = {}
        for issue in error.errors():
            field = ".".join(str(part) for part in issue["loc"]) or "body"
            message = issue["msg"].removeprefix("Value error, ")
            details[field] = f"{details[field]} {message}" if field in details else message
        raise validation_error("The request is invalid.", details) from error


def _int_arg(name: str, default: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= maximum:
        message = f"Must be a whole number from 1 to {maximum}."
        raise validation_error(f"Invalid {name}.", {name: message})
    return int(raw)


def paginate(session: Session, statement: Select, order_by, serialise) -> dict[str, Any]:
    page = _int_arg("page", 1, 10_000)
    page_size = _int_arg("page_size", 20, 100)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    events = session.scalars(
        statement.options(selectinload(Event.ticket_categories))
        .order_by(*order_by)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [serialise(event) for event in events],
        "page": page,
        "page_size": page_size,
        "total_items": total,
    }
