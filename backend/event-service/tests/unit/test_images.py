from io import BytesIO

import pytest

from app.images import MAX_IMAGE_BYTES
from tests.unit.test_events import (
    EVENTS,
    MY_EVENTS,
    OTHER_ORGANISER,
    cancel,
    create_draft,
    error_code,
    publish,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64


def upload(client, headers, event_id, data=PNG, content_type="image/png"):
    return client.put(
        f"{MY_EVENTS}/{event_id}/image",
        data=data,
        content_type=content_type,
        headers=headers,
    )


@pytest.fixture()
def draft(client, organiser_headers, event_payload):
    return create_draft(client, organiser_headers, event_payload)


def test_events_have_no_image_until_one_is_uploaded(draft):
    assert draft["image_url"] is None


@pytest.mark.parametrize(
    ("data", "content_type"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")],
)
def test_organiser_uploads_each_supported_type(
    client, organiser_headers, draft, data, content_type
):
    response = upload(client, organiser_headers, draft["event_id"], data, content_type)

    assert response.status_code == 200
    assert response.get_json()["image_url"] == f"{EVENTS}/{draft['event_id']}/image?v=1"
    preview = client.get(f"{MY_EVENTS}/{draft['event_id']}/image", headers=organiser_headers)
    assert preview.status_code == 200
    assert preview.mimetype == content_type
    assert preview.data == data


def test_replacing_the_image_changes_its_url(client, organiser_headers, draft):
    upload(client, organiser_headers, draft["event_id"], PNG, "image/png")
    response = upload(client, organiser_headers, draft["event_id"], JPEG, "image/jpeg")

    assert response.get_json()["image_url"].endswith("?v=2")
    preview = client.get(f"{MY_EVENTS}/{draft['event_id']}/image", headers=organiser_headers)
    assert preview.data == JPEG


def test_draft_image_is_private_and_published_image_is_public(client, organiser_headers, draft):
    event_id = draft["event_id"]
    upload(client, organiser_headers, event_id)

    assert client.get(f"{EVENTS}/{event_id}/image").status_code == 404

    assert publish(client, organiser_headers, event_id).status_code == 200
    public = client.get(f"{EVENTS}/{event_id}/image")
    assert public.status_code == 200
    assert public.data == PNG
    assert public.mimetype == "image/png"
    assert "immutable" in public.headers["Cache-Control"]
    assert public.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get(f"{EVENTS}/{event_id}").get_json()["image_url"].endswith("?v=1")


def test_published_event_can_change_its_image(client, organiser_headers, draft):
    publish(client, organiser_headers, draft["event_id"])

    response = upload(client, organiser_headers, draft["event_id"])

    assert response.status_code == 200


def test_cancelled_event_image_stays_visible_but_cannot_change(client, organiser_headers, draft):
    event_id = draft["event_id"]
    upload(client, organiser_headers, event_id)
    publish(client, organiser_headers, event_id)
    cancel(client, organiser_headers, event_id)

    assert client.get(f"{EVENTS}/{event_id}/image").status_code == 200
    response = upload(client, organiser_headers, event_id)
    assert response.status_code == 409
    assert error_code(response) == "INVALID_EVENT_STATE"


def test_public_image_of_event_without_one_is_not_found(client, organiser_headers, draft):
    publish(client, organiser_headers, draft["event_id"])

    response = client.get(f"{EVENTS}/{draft['event_id']}/image")

    assert response.status_code == 404
    assert error_code(response) == "IMAGE_NOT_FOUND"


def test_image_over_the_limit_is_rejected(client, organiser_headers, draft):
    data = PNG + b"\x00" * MAX_IMAGE_BYTES

    response = upload(client, organiser_headers, draft["event_id"], data)

    assert response.status_code == 413
    assert error_code(response) == "IMAGE_TOO_LARGE"


def test_image_at_the_limit_is_accepted(client, organiser_headers, draft):
    data = PNG + b"\x00" * (MAX_IMAGE_BYTES - len(PNG))

    assert upload(client, organiser_headers, draft["event_id"], data).status_code == 200


def test_chunked_upload_over_the_limit_is_rejected(client, organiser_headers, draft):
    data = PNG + b"\x00" * MAX_IMAGE_BYTES

    response = client.put(
        f"{MY_EVENTS}/{draft['event_id']}/image",
        input_stream=BytesIO(data),
        content_type="image/png",
        headers=organiser_headers,
    )

    assert response.status_code == 413


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (b"GIF89a" + b"\x00" * 32, "image/gif"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml"),
        (b"<script>alert(1)</script>", "image/png"),
        (JPEG, "image/png"),
        (PNG, "text/html"),
    ],
)
def test_unsupported_or_mislabelled_files_are_rejected(
    client, organiser_headers, draft, data, content_type
):
    response = upload(client, organiser_headers, draft["event_id"], data, content_type)

    assert response.status_code == 415
    assert error_code(response) == "UNSUPPORTED_IMAGE_TYPE"


def test_empty_upload_is_rejected(client, organiser_headers, draft):
    response = upload(client, organiser_headers, draft["event_id"], b"")

    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


def test_only_the_owner_can_change_or_preview_the_image(client, organiser_headers, draft):
    other = organiser_headers | {"X-User-Sub": OTHER_ORGANISER}

    assert upload(client, other, draft["event_id"]).status_code == 404
    upload(client, organiser_headers, draft["event_id"])
    preview = client.get(f"{MY_EVENTS}/{draft['event_id']}/image", headers=other)
    assert preview.status_code == 404


def test_upload_needs_an_organiser(client, draft):
    assert upload(client, {}, draft["event_id"]).status_code == 401
    buyer = {"X-User-Sub": OTHER_ORGANISER, "X-User-Username": "buyer@example.com"}
    assert upload(client, buyer, draft["event_id"]).status_code == 403


def test_image_does_not_change_the_event_version_or_messages(client, organiser_headers, draft):
    response = upload(client, organiser_headers, draft["event_id"])

    assert response.get_json()["status"] == "DRAFT"
    detail = client.get(f"{MY_EVENTS}/{draft['event_id']}", headers=organiser_headers)
    assert detail.get_json()["image_url"] is not None
