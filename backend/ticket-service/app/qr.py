"""Signed QR payloads: `TKT1.<key_id>.<ticket_id>.<signature>`.

The signature is base64url (no padding) of HMAC-SHA256 over `TKT1.<key_id>.<ticket_id>`.
Only Ticket Service holds the secrets, and only Ticket Service verifies (at check-in), so a
symmetric MAC is enough. `key_id` lets keys rotate: new tickets use the active key and older
keys stay configured so tickets already issued keep verifying.
"""

import base64
import hashlib
import hmac
import io
import re
import uuid
from dataclasses import dataclass

import segno

PREFIX = "TKT1"
_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_SIGNATURE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # 32 bytes, base64url without padding
MAX_PAYLOAD_LENGTH = 200


class InvalidQrCode(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedCode:
    ticket_id: str
    key_id: str
    signature: str


class QrSigner:
    def __init__(self, keys: dict[str, str], active_key_id: str):
        if active_key_id not in keys:
            raise ValueError("active_key_id must be one of keys")
        self._keys = {key_id: secret.encode() for key_id, secret in keys.items()}
        self.active_key_id = active_key_id

    def _mac(self, key_id: str, ticket_id: str) -> str:
        message = f"{PREFIX}.{key_id}.{ticket_id}".encode()
        digest = hmac.new(self._keys[key_id], message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def sign(self, ticket_id: str) -> tuple[str, str]:
        """Returns (key_id, signature) for a new ticket."""
        return self.active_key_id, self._mac(self.active_key_id, ticket_id)

    @staticmethod
    def payload(ticket_id: str, key_id: str, signature: str) -> str:
        return f"{PREFIX}.{key_id}.{ticket_id}.{signature}"

    def verify(self, payload: str) -> VerifiedCode:
        if not isinstance(payload, str) or len(payload) > MAX_PAYLOAD_LENGTH:
            raise InvalidQrCode("malformed")
        parts = payload.strip().split(".")
        if len(parts) != 4 or parts[0] != PREFIX:
            raise InvalidQrCode("malformed")
        _, key_id, raw_ticket_id, signature = parts
        if not _KEY_ID.match(key_id) or not _SIGNATURE.match(signature):
            raise InvalidQrCode("malformed")
        try:
            ticket_id = str(uuid.UUID(raw_ticket_id))
        except ValueError as error:
            raise InvalidQrCode("malformed") from error
        if ticket_id != raw_ticket_id or key_id not in self._keys:
            raise InvalidQrCode("unknown key or non-canonical ID")
        if not hmac.compare_digest(self._mac(key_id, ticket_id), signature):
            raise InvalidQrCode("bad signature")
        return VerifiedCode(ticket_id=ticket_id, key_id=key_id, signature=signature)


def render_png(payload: str, scale: int = 8) -> bytes:
    buffer = io.BytesIO()
    segno.make(payload, error="m", micro=False).save(buffer, kind="png", scale=scale, border=4)
    return buffer.getvalue()
