"""Buyer identity for the public API.

The token is verified by API Gateway, which overwrites any client-supplied identity
headers and forwards X-User-Sub (the authenticated Cognito subject). We trust that
header only because the security groups keep this service unreachable from anywhere
other than the gateway. The Stripe webhook route deliberately never calls this.
"""

from dataclasses import dataclass

from flask import Request

from .errors import ApiError, Code


@dataclass(frozen=True)
class Buyer:
    user_id: str
    username: str


def require_buyer(request: Request) -> Buyer:
    """The authenticated buyer from the gateway's X-User-Sub.

    Working assumption (locked 2026-10-19, see the wiki checkout-service-spec page):
    X-User-Sub is the buyer's platform user_id, so it is compared directly against
    the hold's user_id with no lookup. If the group ever decides the sub is a raw
    Cognito id instead, this is the single place the mapping changes.
    """
    user_id = request.headers.get("X-User-Sub", "").strip()
    if not user_id or len(user_id) > 64:
        raise ApiError(401, Code.UNAUTHENTICATED, "Sign in is required.")
    username = request.headers.get("X-User-Username", "").strip()
    return Buyer(user_id=user_id, username=username or user_id)
