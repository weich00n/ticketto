import logging
import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user
from app.cognito import CognitoClient, get_cognito
from app.context import get_correlation_id
from app.db import get_db
from app.errors import unauthenticated, user_not_found
from app.models import ROLE_BUYER, ROLE_ORGANISER, User
from app.schemas import ErrorResponse, OrganiserUpgradeResponse, UserPatch, UserResponse

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/users",
    tags=["users"],
    responses={401: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


def _find_by_sub(db: Session, sub: str) -> User | None:
    return db.scalar(select(User).where(User.cognito_sub == sub))


def _require_by_sub(db: Session, sub: str) -> User:
    user = _find_by_sub(db, sub)
    if user is None:
        raise user_not_found()
    return user


def _response(user: User) -> UserResponse:
    return UserResponse.from_user(user, get_correlation_id())


@router.get("/me", response_model=UserResponse, responses={404: {"model": ErrorResponse}})
def get_me(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserResponse:
    return _response(_require_by_sub(db, current.sub))


@router.post(
    "/me",
    response_model=UserResponse,
    status_code=201,
    responses={200: {"model": UserResponse, "description": "Profile already existed"}},
)
def create_me(
    response: Response,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    cognito: CognitoClient = Depends(get_cognito),
) -> UserResponse:
    """Create the caller's profile on first login. Safe to call on every login."""
    existing = _find_by_sub(db, current.sub)
    if existing is not None:
        response.status_code = 200
        return _response(existing)

    # email and name come from Cognito, not the request body, so they can't be faked.
    attributes = cognito.get_user_attributes(current.username)
    if attributes.get("sub") and attributes["sub"] != current.sub:
        logger.warning("Cognito sub does not match X-User-Sub")
        raise unauthenticated()
    email = attributes.get("email")
    if not email:
        raise RuntimeError("Cognito user has no email attribute")
    # `name` is required at sign-up; fall back for users created without it (e.g. by an admin).
    name = attributes.get("name") or email.split("@", 1)[0]

    user = User(
        user_id=str(uuid.uuid4()),
        cognito_sub=current.sub,
        email=email,
        name=name,
        role=ROLE_BUYER,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Another request created this profile between our lookup and insert.
        db.rollback()
        existing = _find_by_sub(db, current.sub)
        if existing is None:
            raise
        response.status_code = 200
        return _response(existing)
    db.refresh(user)
    logger.info("User profile created", extra={"user_id": user.user_id})
    return _response(user)


@router.patch(
    "/me",
    response_model=UserResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def update_me(
    body: UserPatch,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    cognito: CognitoClient = Depends(get_cognito),
) -> UserResponse:
    """Update the caller's name (in Cognito first, then the database) and/or their
    new-event email setting (database only)."""
    user = _require_by_sub(db, current.sub)
    changed = False
    if body.name is not None and body.name != user.name:
        # Cognito first: both steps are idempotent, so a retry after a DB failure converges.
        cognito.update_name(current.username, body.name)
        user.name = body.name
        changed = True
    if body.notify_new_events is not None and body.notify_new_events != user.notify_new_events:
        user.notify_new_events = body.notify_new_events
        changed = True
    if changed:
        db.commit()
        db.refresh(user)
    return _response(user)


@router.post(
    "/me/organiser",
    response_model=OrganiserUpgradeResponse,
    responses={404: {"model": ErrorResponse}},
)
def become_organiser(
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    cognito: CognitoClient = Depends(get_cognito),
) -> OrganiserUpgradeResponse:
    """Upgrade the caller to organiser immediately (no approval)."""
    user = _require_by_sub(db, current.sub)

    # Cognito first, then the DB: see cognito-settings.md §7.
    cognito.add_to_organiser_group(current.username)
    if user.role != ROLE_ORGANISER:
        user.role = ROLE_ORGANISER
        db.commit()
        db.refresh(user)
        logger.info("User upgraded to organiser", extra={"user_id": user.user_id})

    return OrganiserUpgradeResponse.from_user(
        user,
        get_correlation_id(),
        token_refresh_required=ROLE_ORGANISER not in current.groups,
    )
