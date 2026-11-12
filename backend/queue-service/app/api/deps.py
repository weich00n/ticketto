"""Request dependencies: buyer identity and the app's shared services."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.errors import Unauthenticated
from app.core.config import Settings
from app.core.tokens import AdmissionTokens
from app.queue.admin import AdminService
from app.queue.config_repo import ConfigRepository
from app.queue.repository import QueueRepository
from app.queue.service import QueueService


@dataclass(frozen=True)
class Services:
    """Everything built once at startup (see app.main lifespan)."""

    settings: Settings
    redis: Redis
    engine: AsyncEngine
    configs: ConfigRepository
    queues: QueueRepository
    tokens: AdmissionTokens
    queue_service: QueueService
    admin_service: AdminService


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def get_queue_service(services: Annotated[Services, Depends(get_services)]) -> QueueService:
    return services.queue_service


def get_admin_service(services: Annotated[Services, Depends(get_services)]) -> AdminService:
    return services.admin_service


def get_tokens(services: Annotated[Services, Depends(get_services)]) -> AdmissionTokens:
    return services.tokens


async def internal_auth() -> None:
    """Guard for /internal/v1. A no-op for now: the endpoints are reachable only
    inside the cluster network. Whether to add a service token is an open point;
    if so, it goes here and nowhere else."""


async def current_user(
    x_user_sub: Annotated[str | None, Header(alias="X-User-Sub")] = None,
) -> str:
    """The buyer's id from X-User-Sub, as a canonical lowercase UUID string.

    This is the single place identity is read; swap it for API-gateway JWT
    claims later without touching the routes.
    """
    if not x_user_sub:
        raise Unauthenticated()
    try:
        return str(uuid.UUID(x_user_sub.strip()))
    except ValueError as exc:
        raise Unauthenticated() from exc


CurrentUser = Annotated[str, Depends(current_user)]
QueueServiceDep = Annotated[QueueService, Depends(get_queue_service)]
AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]
TokensDep = Annotated[AdmissionTokens, Depends(get_tokens)]
ServicesDep = Annotated[Services, Depends(get_services)]
