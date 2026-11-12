import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import get_db

logger = logging.getLogger(__name__)

# Not under /api/v1 and not exposed through API Gateway; only the ALB / ECS call these.
router = APIRouter(tags=["health"])


@router.get("/health")
def liveness() -> dict:
    """The process is up. Does not touch the database, so a DB outage doesn't
    make ECS restart healthy tasks."""
    return {"status": "ok"}


@router.get("/health/ready")
def readiness(db: Session = Depends(get_db)) -> JSONResponse:
    """The service can reach MySQL."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("Readiness check failed: database unreachable", exc_info=True)
        return JSONResponse(
            status_code=503, content={"status": "unavailable", "checks": {"database": "down"}}
        )
    return JSONResponse(content={"status": "ok", "checks": {"database": "up"}})
