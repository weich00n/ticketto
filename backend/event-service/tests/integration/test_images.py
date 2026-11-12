import pytest
from sqlalchemy import text

from app.images import MAX_IMAGE_BYTES

pytestmark = pytest.mark.integration

MY_EVENTS = "/api/v1/organizer/events"
PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def png(size: int) -> bytes:
    """A PNG-looking body of `size` bytes that cycles through every byte value."""
    body = bytes(range(256)) * (size // 256 + 1)
    return (PNG_HEADER + body)[:size]


def draft(client, organiser, event_payload):
    created = client.post(MY_EVENTS, json=event_payload, headers=organiser)
    assert created.status_code == 201, created.text
    return created.json()["event_id"]


def upload(client, organiser, event_id, data, content_type="image/png"):
    return client.put(
        f"{MY_EVENTS}/{event_id}/image",
        content=data,
        headers={**organiser, "Content-Type": content_type},
    )


def stored(engine, event_id):
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT content_type, LENGTH(data) FROM event_images WHERE event_id = :id"),
            {"id": event_id},
        ).all()
        version = connection.execute(
            text("SELECT image_version FROM events WHERE event_id = :id"), {"id": event_id}
        ).scalar()
    return rows, version


def test_image_bytes_survive_the_database_and_are_served_publicly(
    client, engine, organiser, event_payload
):
    event_id = draft(client, organiser, event_payload)
    data = png(5000)  # every byte value, so any text-mode handling would corrupt it

    assert upload(client, organiser, event_id, data).status_code == 200
    rows, version = stored(engine, event_id)
    assert rows == [("image/png", len(data))]
    assert version == 1

    preview = client.get(f"{MY_EVENTS}/{event_id}/image", headers=organiser)
    assert preview.status_code == 200
    assert preview.content == data

    # A draft's image is private; publishing makes it public.
    assert client.get(f"/api/v1/events/{event_id}/image").status_code == 404
    assert client.post(f"{MY_EVENTS}/{event_id}/publish", headers=organiser).status_code == 200
    public = client.get(f"/api/v1/events/{event_id}/image")
    assert public.status_code == 200
    assert public.content == data
    assert public.headers["content-type"] == "image/png"
    assert "immutable" in public.headers["cache-control"]
    assert client.get(f"/api/v1/events/{event_id}").json()["image_url"].endswith("?v=1")


def test_replacing_an_image_keeps_one_row_and_bumps_the_version(
    client, engine, organiser, event_payload
):
    event_id = draft(client, organiser, event_payload)
    upload(client, organiser, event_id, png(1000))
    jpeg = b"\xff\xd8\xff\xe0" + b"\x01" * 200

    response = upload(client, organiser, event_id, jpeg, "image/jpeg")

    assert response.status_code == 200
    assert response.json()["image_url"].endswith("?v=2")
    rows, version = stored(engine, event_id)
    assert rows == [("image/jpeg", len(jpeg))]
    assert version == 2


def test_the_size_limit_holds_through_the_real_server(client, engine, organiser, event_payload):
    event_id = draft(client, organiser, event_payload)

    assert upload(client, organiser, event_id, png(MAX_IMAGE_BYTES)).status_code == 200

    too_big = upload(client, organiser, event_id, png(MAX_IMAGE_BYTES + 1))
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "IMAGE_TOO_LARGE"
    rows, version = stored(engine, event_id)
    assert rows == [("image/png", MAX_IMAGE_BYTES)]  # the rejected upload changed nothing
    assert version == 1


def test_a_file_that_is_not_an_image_is_rejected(client, engine, organiser, event_payload):
    event_id = draft(client, organiser, event_payload)

    response = upload(client, organiser, event_id, b"<html>not an image</html>")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_IMAGE_TYPE"
    assert stored(engine, event_id) == ([], 0)
