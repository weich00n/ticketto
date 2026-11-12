import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, literal, select
from sqlalchemy.orm import Session

from app.context import get_correlation_id
from app.db import get_db
from app.errors import ApiError
from app.models import User
from app.schemas import ErrorResponse, UserListResponse, UserResponse, UserSummary

# Service-to-service routes. These have NO API Gateway route; they are reachable only
# from inside the VPC, and network rules (security groups) restrict who can call them.
router = APIRouter(prefix="/internal/v1/users", tags=["internal"])

MAX_PAGE_SIZE = 1000


def _validation_error(message: str) -> ApiError:
    return ApiError(400, "VALIDATION_ERROR", message)


@router.get("", response_model=UserListResponse, responses={400: {"model": ErrorResponse}})
def list_users(
    notify_new_events: bool | None = None,
    purchased_event_id: uuid.UUID | None = None,
    after: uuid.UUID | None = None,
    limit: int = Query(500, ge=1, le=MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
) -> UserListResponse:
    """Who notification-service should email, a page at a time, ordered by user_id.

    Exactly one filter: `notify_new_events=true` (users who want new-event emails) or
    `purchased_event_id=<uuid>` (users who bought tickets for that event, whatever their
    setting). Pass the previous page's `next_after` as `after` for the next page.
    """
    if (notify_new_events is None) == (purchased_event_id is None):
        raise _validation_error("Pass exactly one of notify_new_events or purchased_event_id.")
    if notify_new_events is False:
        raise _validation_error("Only notify_new_events=true can be listed.")

    query = select(User).order_by(User.user_id).limit(limit)
    if notify_new_events:
        query = query.where(User.notify_new_events.is_(True))
    else:
        # Exact match inside the comma-separated list: ",a,b," contains ",a,".
        padded = literal(",", String) + User.purchased_event_ids + literal(",", String)
        query = query.where(padded.like(f"%,{purchased_event_id},%"))
    if after is not None:
        query = query.where(User.user_id > str(after))

    users = db.scalars(query).all()
    items = [
        UserSummary(user_id=u.user_id, cognito_sub=u.cognito_sub, email=u.email, name=u.name)
        for u in users
    ]
    # A full page may be followed by more; an empty next page ends the listing.
    next_after = items[-1].user_id if len(items) == limit else None
    return UserListResponse(items=items, next_after=next_after)


@router.get(
    "/by-sub/{sub}",
    response_model=UserResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def get_user_by_sub(sub: uuid.UUID, db: Session = Depends(get_db)) -> UserResponse:
    """Look a user up by Cognito sub: the `user_id` that checkout-service's messages carry."""
    user = db.scalar(select(User).where(User.cognito_sub == str(sub)))
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found.")
    return UserResponse.from_user(user, get_correlation_id())


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def get_user(user_id: uuid.UUID, db: Session = Depends(get_db)) -> UserResponse:
    user = db.get(User, str(user_id))
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found.")
    return UserResponse.from_user(user, get_correlation_id())
