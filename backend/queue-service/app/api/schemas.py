"""HTTP response bodies. Shapes follow docs/Endpoint reference.md exactly.

QueueEntryResponse is serialised with exclude_unset, so a field appears only when
the handler set it: `position: null` is sent for a pre-queue entry, but omitted
for an admitted one, and poll_after_seconds (required) is always present.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.core.status import EntryStatus, QueueStatus


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any]


class ErrorBody(BaseModel):
    error: ErrorDetail


class AdmissionBody(BaseModel):
    admission_id: str
    user_id: str
    event_id: str
    admitted_at: str
    selection_expires_at: str
    admission_expires_at: str


class LobbyResponse(BaseModel):
    event_id: str
    queue_status: QueueStatus
    queue_opens_at: str
    release_at: str
    sale_ends_at: str | None
    server_time: str


class QueueEntryResponse(BaseModel):
    event_id: str
    queue_status: QueueStatus
    entry_status: EntryStatus
    position: int | None = None
    release_at: str | None = None
    admission: AdmissionBody | None = None
    admission_token: str | None = None
    poll_after_seconds: int | None


class QueueListItem(BaseModel):
    event_id: str
    queue_status: QueueStatus
    release_at: str


class QueueListResponse(BaseModel):
    items: list[QueueListItem]
    next_cursor: str | None


class QueueDetailResponse(BaseModel):
    event_id: str
    queue_status: QueueStatus
    event_version: int
    total_capacity: int
    active_pool_capacity: int
    queue_opens_at: str
    release_at: str
    sale_ends_at: str | None
    closed_at: str | None
    waiting_count: int
    active_count: int


class AdmissionListResponse(BaseModel):
    items: list[AdmissionBody]
    next_cursor: str | None


class Jwk(BaseModel):
    kty: str
    kid: str
    use: str
    alg: str
    n: str
    e: str


class JwksResponse(BaseModel):
    keys: list[Jwk]


class LiveResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    """200 {"status": "ready"}; 503 {"status": "unavailable", "failed": [...]}."""

    status: str
    failed: list[str] | None = None


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses=` entry documenting the shared error body."""
    return {status: {"model": ErrorBody} for status in statuses}
