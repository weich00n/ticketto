import re
from dataclasses import dataclass

from flask import Request

from .errors import ApiError


@dataclass(frozen=True)
class Actor:
    organiser_id: str
    username: str
    groups: frozenset[str]


def _groups(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    # The gateway sends the claim as "[admin organiser]" or "admin,organiser".
    names = (group.strip('"') for group in re.split(r"[,\s]+", value.strip().strip("[]")))
    return frozenset(name for name in names if name)


def require_organiser(request: Request) -> Actor:
    """Identity comes from the headers API Gateway sets after validating the token."""
    organiser_id = request.headers.get("X-User-Sub", "").strip()
    username = request.headers.get("X-User-Username", "").strip()
    if not organiser_id or not username or len(organiser_id) > 64:
        raise ApiError(401, "UNAUTHENTICATED", "Sign in is required.")
    actor = Actor(
        organiser_id=organiser_id,
        username=username,
        groups=_groups(request.headers.get("X-User-Groups")),
    )
    if "organiser" not in actor.groups:
        raise ApiError(403, "FORBIDDEN", "Organiser access is required.")
    return actor
