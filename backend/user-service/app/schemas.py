from datetime import UTC, datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StringConstraints,
    field_serializer,
    model_validator,
)

from app.models import User


def to_iso_utc(value: datetime) -> str:
    """Format as ISO 8601 UTC with a Z suffix, e.g. 2026-11-20T12:00:00Z.

    The DB session runs in UTC, so naive datetimes from MySQL are already UTC.
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class UserResponse(BaseModel):
    user_id: str
    email: str
    name: str
    role: str
    notify_new_events: bool
    created_at: datetime
    updated_at: datetime
    correlation_id: str

    @field_serializer("created_at", "updated_at")
    def _serialize_timestamp(self, value: datetime) -> str:
        return to_iso_utc(value)

    @classmethod
    def from_user(cls, user: User, correlation_id: str, **extra):
        return cls(
            user_id=user.user_id,
            email=user.email,
            name=user.name,
            role=user.role,
            notify_new_events=user.notify_new_events,
            created_at=user.created_at,
            updated_at=user.updated_at,
            correlation_id=correlation_id,
            **extra,
        )


class OrganiserUpgradeResponse(UserResponse):
    token_refresh_required: bool


class UserPatch(BaseModel):
    # Unknown fields (role, email, user_id, purchased_event_ids, ...) are rejected with 400.
    model_config = ConfigDict(extra="forbid")

    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
        | None
    ) = None
    # Strict: only JSON true/false, not "yes" or 1.
    notify_new_events: StrictBool | None = None

    @model_validator(mode="after")
    def _at_least_one_value(self) -> "UserPatch":
        if not self.model_fields_set:
            raise ValueError("Send name and/or notify_new_events.")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self


class UserSummary(BaseModel):
    user_id: str
    # notification-service stores it with each notification, to match a signed-in user's token.
    cognito_sub: str
    email: str
    name: str


class UserListResponse(BaseModel):
    items: list[UserSummary]
    # Pass as `after` to get the next page; null on the last page.
    next_after: str | None


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
    correlation_id: str
