"""Opaque pagination cursors: base64url-encoded compact JSON objects."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any


class InvalidCursor(ValueError):
    pass


def encode_cursor(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor("malformed cursor") from exc
    if not isinstance(data, dict):
        raise InvalidCursor("malformed cursor")
    return data
