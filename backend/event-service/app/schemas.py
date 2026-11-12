from datetime import UTC, datetime, timedelta
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationInfo, field_validator

from .status import EventStatus

EVENT_STATUSES = tuple(EventStatus)

EventCategory = Literal["Concerts", "Theatre", "Festivals", "Sports", "Conferences"]
EVENT_CATEGORIES = get_args(EventCategory)

# Buyers can join the queue this long before tickets are released.
QUEUE_JOIN_LEAD = timedelta(minutes=15)


def queue_join_opens_at(ticket_release_at: datetime) -> datetime:
    return ticket_release_at - QUEUE_JOIN_LEAD


def _non_blank(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Must not be blank.")
    return value


def _future_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Must include a UTC offset.")
    value = value.astimezone(UTC)
    if value <= datetime.now(UTC):
        raise ValueError("Must be in the future.")
    return value


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TicketCategoryInput(ApiModel):
    name: str = Field(min_length=1, max_length=80)
    price_amount: StrictInt = Field(ge=0, le=100_000_000)
    currency: Literal["SGD"] = "SGD"
    total_quantity: StrictInt = Field(ge=1, le=1_000_000)

    strip_name = field_validator("name")(_non_blank)


class EventInput(ApiModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=5000)
    venue: str = Field(min_length=1, max_length=200)
    category: EventCategory | None = None
    starts_at: datetime
    ticket_release_at: datetime
    ticket_categories: list[TicketCategoryInput] = Field(min_length=1, max_length=10)

    strip_text = field_validator("title", "description", "venue")(_non_blank)
    check_starts_at = field_validator("starts_at")(_future_utc)

    @field_validator("ticket_release_at")
    @classmethod
    def release_before_start(cls, value: datetime, info: ValidationInfo) -> datetime:
        value = _future_utc(value)
        starts_at = info.data.get("starts_at")
        if starts_at is not None and value >= starts_at:
            raise ValueError("Must be before starts_at.")
        return value

    @field_validator("ticket_categories")
    @classmethod
    def unique_names(cls, value: list[TicketCategoryInput]) -> list[TicketCategoryInput]:
        names = [category.name.casefold() for category in value]
        if len(names) != len(set(names)):
            raise ValueError("Category names must be unique within the event.")
        return value


class CancelInput(ApiModel):
    reason: str = Field(min_length=1, max_length=500)

    strip_reason = field_validator("reason")(_non_blank)
