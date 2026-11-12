"""Who is calling.

No authentication yet (AUTH_MODE=none, the default): buyers identify themselves with an
X-User-Id header, the interim approach Queue Service uses, and check-in is open to any caller.

AUTH_MODE=gateway is for when API Gateway and Cognito are in front: identity then comes from
the headers the gateway sets after validating the token (X-User-Sub, X-User-Username,
X-User-Groups), and check-in needs one of CHECK_IN_GROUPS (default `organiser` or `staff`;
the team's Cognito roles are `buyer` and `organiser`).
"""

import re
from dataclasses import dataclass

from flask import Request

from .errors import ApiError

AUTH_NONE = "none"
AUTH_GATEWAY = "gateway"
AUTH_MODES = (AUTH_NONE, AUTH_GATEWAY)


@dataclass(frozen=True)
class Caller:
    user_id: str  # must equal the user_id Checkout sends in OrderConfirmed
    username: str | None
    groups: frozenset[str]


def _groups(value: str | None) -> frozenset[str]:
    # API Gateway sends Cognito groups as a string such as "[staff]", "[buyer, staff]" or
    # "[buyer staff]".
    if not value:
        return frozenset()
    return frozenset(
        group.strip('"') for group in re.split(r"[,\s]+", value.strip().strip("[]")) if group
    )


def _header(request: Request, name: str, limit: int) -> str | None:
    value = request.headers.get(name, "").strip()
    return value[:limit] if value else None


def require_user(request: Request, mode: str) -> Caller:
    if mode == AUTH_GATEWAY:
        user_id = request.headers.get("X-User-Sub", "").strip()
        if not user_id or len(user_id) > 64:
            raise ApiError(401, "UNAUTHENTICATED", "Sign in is required.")
        return Caller(
            user_id=user_id,
            username=_header(request, "X-User-Username", 128),
            groups=_groups(request.headers.get("X-User-Groups")),
        )
    user_id = request.headers.get("X-User-Id", "").strip()
    if not user_id or len(user_id) > 64:
        raise ApiError(401, "UNAUTHENTICATED", "The X-User-Id header is required.")
    return Caller(user_id=user_id, username=None, groups=frozenset())


def check_in_caller(request: Request, mode: str, allowed_groups: frozenset[str]) -> Caller:
    """With gateway auth, only members of `allowed_groups` (organisers or staff) may check
    tickets in. Without it, anyone may; an X-User-Id header, if sent, is recorded as who
    scanned the ticket."""
    if mode == AUTH_GATEWAY:
        caller = require_user(request, mode)
        if not caller.groups & allowed_groups:
            raise ApiError(403, "FORBIDDEN", "Only event organisers or staff can check tickets in.")
        return caller
    scanner = _header(request, "X-User-Id", 64)
    return Caller(user_id=scanner or "", username=scanner, groups=frozenset())
