"""Internal endpoints: queue admin views (/internal/v1), token public keys and
health checks. Reachable only inside the cluster network."""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import AdminServiceDep, ServicesDep, TokensDep, internal_auth
from app.api.schemas import (
    AdmissionBody,
    AdmissionListResponse,
    JwksResponse,
    LiveResponse,
    QueueDetailResponse,
    QueueListItem,
    QueueListResponse,
    ReadyResponse,
    error_responses,
)
from app.core.status import QueueStatus
from app.core.timefmt import to_iso
from app.queue.admin import DEFAULT_PAGE_SIZE

# Readiness must answer fast, so a slow dependency fails the check instead of
# hanging the load balancer's probe.
READY_TIMEOUT_SECONDS = 1.0
JWKS_CACHE_CONTROL = "public, max-age=300"

router = APIRouter(prefix="/internal/v1", tags=["internal"], dependencies=[Depends(internal_auth)])
ops_router = APIRouter(tags=["ops"])

Limit = Annotated[int, Query(description="Page size; clamped to 1..200.")]
Cursor = Annotated[str | None, Query(description="next_cursor from the previous page.")]


@router.get(
    "/queues",
    response_model=QueueListResponse,
    responses=error_responses(400, 503),
    summary="List queue configurations, filterable by status",
)
async def list_queues(
    service: AdminServiceDep,
    status: Annotated[QueueStatus | None, Query()] = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> QueueListResponse:
    page = await service.list_queues(status, limit, cursor)
    return QueueListResponse(
        items=[
            QueueListItem(
                event_id=item.cfg.event_id,
                queue_status=item.queue_status,
                release_at=to_iso(item.cfg.release_at),
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/events/{event_id}/queue",
    response_model=QueueDetailResponse,
    responses=error_responses(404, 503),
    summary="One queue's configuration and live counts",
)
async def get_queue(event_id: UUID, service: AdminServiceDep) -> QueueDetailResponse:
    detail = await service.queue_detail(str(event_id))
    cfg = detail.cfg
    return QueueDetailResponse(
        event_id=cfg.event_id,
        queue_status=detail.queue_status,
        event_version=cfg.event_version,
        total_capacity=cfg.total_capacity,
        active_pool_capacity=cfg.active_pool_capacity,
        queue_opens_at=to_iso(cfg.queue_opens_at),
        release_at=to_iso(cfg.release_at),
        sale_ends_at=to_iso(cfg.sale_ends_at) if cfg.sale_ends_at else None,
        closed_at=to_iso(cfg.closed_at) if cfg.closed_at else None,
        waiting_count=detail.counts.waiting,
        active_count=detail.counts.active,
    )


@router.get(
    "/events/{event_id}/queue/admissions",
    response_model=AdmissionListResponse,
    responses=error_responses(400, 404, 503),
    summary="Active admissions for an event, oldest first",
)
async def list_admissions(
    event_id: UUID,
    service: AdminServiceDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> AdmissionListResponse:
    page = await service.admissions(str(event_id), limit, cursor)
    return AdmissionListResponse(
        items=[AdmissionBody(**a.to_public()) for a in page.items],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/admissions/{admission_id}",
    response_model=AdmissionBody,
    responses=error_responses(404, 503),
    summary="Look up one admission (never includes the token)",
)
async def get_admission(admission_id: UUID, service: AdminServiceDep) -> AdmissionBody:
    admission = await service.admission(str(admission_id))
    return AdmissionBody(**admission.to_public())


@ops_router.get(
    "/.well-known/jwks.json",
    response_model=JwksResponse,
    summary="Public keys for verifying admission tokens",
)
async def jwks(tokens: TokensDep) -> JSONResponse:
    return JSONResponse(tokens.jwks(), headers={"Cache-Control": JWKS_CACHE_CONTROL})


@ops_router.get("/health/live", response_model=LiveResponse, summary="Process is running")
async def live() -> LiveResponse:
    return LiveResponse(status="ok")


@ops_router.get(
    "/health/ready",
    response_model=ReadyResponse,
    response_model_exclude_none=True,
    responses={503: {"model": ReadyResponse}},
    summary="MySQL and Redis are reachable",
)
async def ready(services: ServicesDep) -> JSONResponse:
    async def redis_ok() -> None:
        await services.redis.ping()

    async def mysql_ok() -> None:
        async with services.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    checks = {"mysql": mysql_ok(), "redis": redis_ok()}
    results = await asyncio.gather(
        *(asyncio.wait_for(check, READY_TIMEOUT_SECONDS) for check in checks.values()),
        return_exceptions=True,
    )
    failed = [name for name, result in zip(checks, results, strict=True) if result is not None]
    if failed:
        return JSONResponse({"status": "unavailable", "failed": failed}, status_code=503)
    return JSONResponse({"status": "ready"})
