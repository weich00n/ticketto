"""Buyer endpoints (/api/v1): lobby info, join, poll own status, leave."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, QueueServiceDep
from app.api.schemas import AdmissionBody, LobbyResponse, QueueEntryResponse, error_responses
from app.core.status import EntryStatus
from app.core.timefmt import to_iso
from app.queue.service import EntryView

router = APIRouter(prefix="/api/v1", tags=["buyer"])


@router.get(
    "/events/{event_id}/queue",
    response_model=LobbyResponse,
    responses=error_responses(404, 503),
    summary="Queue status and times for the lobby countdown",
)
async def get_queue_info(event_id: UUID, service: QueueServiceDep) -> LobbyResponse:
    lobby = await service.lobby(str(event_id))
    return LobbyResponse(
        event_id=lobby.cfg.event_id,
        queue_status=lobby.queue_status,
        queue_opens_at=to_iso(lobby.cfg.queue_opens_at),
        release_at=to_iso(lobby.cfg.release_at),
        sale_ends_at=to_iso(lobby.cfg.sale_ends_at) if lobby.cfg.sale_ends_at else None,
        server_time=to_iso(lobby.server_time),
    )


@router.post(
    "/events/{event_id}/queue/entries",
    status_code=status.HTTP_201_CREATED,
    response_model=QueueEntryResponse,
    response_model_exclude_unset=True,
    responses={200: {"model": QueueEntryResponse}, **error_responses(401, 404, 409, 410, 503)},
    summary="Join the queue (idempotent)",
)
async def join_queue(
    event_id: UUID, user_id: CurrentUser, service: QueueServiceDep, response: Response
) -> QueueEntryResponse:
    created, view = await service.join(str(event_id), user_id)
    if not created:
        response.status_code = status.HTTP_200_OK
    return _entry_response(view)


@router.get(
    "/events/{event_id}/queue/me",
    response_model=QueueEntryResponse,
    response_model_exclude_unset=True,
    responses=error_responses(401, 404, 503),
    summary="Poll own position or admission",
)
async def get_my_entry(
    event_id: UUID, user_id: CurrentUser, service: QueueServiceDep
) -> QueueEntryResponse:
    return _entry_response(await service.status(str(event_id), user_id))


@router.delete(
    "/events/{event_id}/queue/me",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=error_responses(401, 404, 409, 503),
    summary="Give up queue position",
)
async def leave_queue(event_id: UUID, user_id: CurrentUser, service: QueueServiceDep) -> Response:
    await service.leave(str(event_id), user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _entry_response(view: EntryView) -> QueueEntryResponse:
    """Build the body field by field: only fields set here are serialised."""
    entry = view.entry
    body = QueueEntryResponse(
        event_id=view.cfg.event_id,
        queue_status=view.queue_status,
        entry_status=entry.status,
        poll_after_seconds=view.poll_after_seconds,
    )
    if entry.status is EntryStatus.WAITING:
        body.position = entry.position
        if entry.position is None:  # pre-queue: order decided at release
            body.release_at = to_iso(view.cfg.release_at)
    elif entry.status is EntryStatus.ADMITTED and entry.admission is not None:
        body.admission = AdmissionBody(**entry.admission.to_public())
        body.admission_token = view.admission_token
    return body
