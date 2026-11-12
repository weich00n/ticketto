from __future__ import annotations

import base64

import pytest

from app.core.cursor import InvalidCursor, decode_cursor, encode_cursor

pytestmark = pytest.mark.unit


def test_round_trip_is_url_safe_and_unpadded() -> None:
    data = {"r": 1_795_176_000_000, "e": "c4a1e8f2-91d0-4b6a-a3e5-7e2b5d8f6c90"}
    cursor = encode_cursor(data)
    assert "=" not in cursor and "+" not in cursor and "/" not in cursor
    assert decode_cursor(cursor) == data


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!!",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(b"[1, 2]").decode(),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        "",
    ],
)
def test_malformed_cursor_rejected(cursor: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor)
