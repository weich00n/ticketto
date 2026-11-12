"""Stand-in for the Reservation service's side of the admission-token contract.

validate_admission_token applies, in order, the checks docs/Endpoint reference.md
tells Reservation to make. Tests use it to prove that the tokens Queue issues
pass, and that forged, tampered or misused ones fail.

FakeReservationService publishes EventSoldOut, as Reservation does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import jwt

from app.core.timefmt import parse_iso
from tests.fakes.publisher import FakeService

CLAIMS = (
    "admission_id",
    "user_id",
    "event_id",
    "admitted_at",
    "selection_expires_at",
    "admission_expires_at",
)


class TokenRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ValidatedAdmission:
    admission_id: str
    user_id: str
    event_id: str
    selection_expires_at: datetime
    admission_expires_at: datetime


def validate_admission_token(
    token: str,
    jwks: dict[str, Any],
    *,
    user_id: str,
    event_id: str,
    now: datetime,
) -> ValidatedAdmission:
    """Validate a token the way Reservation must before creating a hold."""
    # 1. Header: RS256 only, and a kid we can find in the JWKS.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.DecodeError as exc:
        raise TokenRejected("malformed") from exc
    if header.get("alg") != "RS256":
        raise TokenRejected("algorithm")
    keys = {k["kid"]: k for k in jwks.get("keys", []) if k.get("alg") == "RS256"}
    jwk = keys.get(header.get("kid", ""))
    if jwk is None:
        raise TokenRejected("unknown_kid")  # real Reservation refetches the JWKS once first

    # 2. Signature, with the algorithm pinned (never taken from the token).
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            jwt.PyJWK.from_json(json.dumps(jwk)).key,
            algorithms=["RS256"],
            options={"require": list(CLAIMS)},
        )
    except jwt.InvalidSignatureError as exc:
        raise TokenRejected("signature") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise TokenRejected("claims") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenRejected("malformed") from exc

    # 3-4. The token belongs to this buyer and this event.
    if claims["user_id"] != user_id:
        raise TokenRejected("user_mismatch")
    if claims["event_id"] != event_id:
        raise TokenRejected("event_mismatch")

    # 5. Holds may only be created before the selection deadline.
    selection_expires_at = parse_iso(claims["selection_expires_at"])
    if now >= selection_expires_at:
        raise TokenRejected("selection_expired")

    return ValidatedAdmission(
        admission_id=claims["admission_id"],
        user_id=claims["user_id"],
        event_id=claims["event_id"],
        selection_expires_at=selection_expires_at,
        admission_expires_at=parse_iso(claims["admission_expires_at"]),
    )


class FakeReservationService(FakeService):
    exchange = "reservation.events"
