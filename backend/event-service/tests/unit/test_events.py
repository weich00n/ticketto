from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.config import Settings
from app.lifecycle import complete_past_events
from app.main import create_app
from app.models import Event, OutboxMessage
from tests.unit.conftest import iso

EVENTS = "/api/v1/events"
MY_EVENTS = "/api/v1/organizer/events"
OTHER_ORGANISER = "22222222-2222-2222-2222-222222222222"


def create_draft(client, headers, payload):
    response = client.post(MY_EVENTS, json=payload, headers=headers)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def publish(client, headers, event_id):
    return client.post(f"{MY_EVENTS}/{event_id}/publish", headers=headers)


def cancel(client, headers, event_id, reason="Venue closure"):
    return client.post(f"{MY_EVENTS}/{event_id}/cancel", json={"reason": reason}, headers=headers)


def outbox(session_factory, message_type=None):
    with session_factory() as session:
        statement = select(OutboxMessage).order_by(OutboxMessage.occurred_at)
        if message_type:
            statement = statement.where(OutboxMessage.message_type == message_type)
        return session.scalars(statement).all()


def error_code(response):
    return response.get_json()["error"]["code"]


# Health, errors and correlation IDs


def test_health_and_readiness(client):
    assert client.get("/health").get_json() == {"status": "ok", "service": "event-service"}
    assert client.get("/ready").get_json() == {"status": "ready"}


def test_unknown_route_uses_the_error_format(client):
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert error_code(response) == "NOT_FOUND"


def test_database_down_is_a_503(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/missing/db", rabbitmq_url="amqp://x")
    client = create_app(settings).test_client()
    response = client.get(EVENTS)
    assert response.status_code == 503
    assert error_code(response) == "SERVICE_UNAVAILABLE"
    assert client.get("/ready").status_code == 503


def test_correlation_id_is_echoed_and_in_error_bodies(client, event_payload):
    response = client.post(MY_EVENTS, json=event_payload, headers={"X-Correlation-Id": "trace-1"})
    assert response.headers["X-Correlation-Id"] == "trace-1"
    assert response.get_json()["correlation_id"] == "trace-1"


def test_unsafe_correlation_id_is_replaced(client):
    response = client.get("/health", headers={"X-Correlation-Id": "bad id <script>"})
    assert response.headers["X-Correlation-Id"] not in ("", "bad id <script>")


# Authentication


def test_organiser_routes_need_identity_headers(client, event_payload):
    response = client.post(MY_EVENTS, json=event_payload)
    assert response.status_code == 401
    assert error_code(response) == "UNAUTHENTICATED"


def test_organiser_routes_need_the_organiser_group(client, organiser_headers, event_payload):
    headers = {**organiser_headers, "X-User-Groups": "[buyer]"}
    response = client.post(MY_EVENTS, json=event_payload, headers=headers)
    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN"


def test_other_organisers_events_are_not_found(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    other = {**organiser_headers, "X-User-Sub": OTHER_ORGANISER}
    url = f"{MY_EVENTS}/{event['event_id']}"
    assert client.get(url, headers=other).status_code == 404
    assert client.put(url, json=event_payload, headers=other).status_code == 404
    assert client.post(f"{url}/publish", headers=other).status_code == 404


# Validation


@pytest.mark.parametrize("price_amount", ["88.00", 88.5, -1, True])
def test_price_amount_is_a_non_negative_integer(
    client, organiser_headers, event_payload, price_amount
):
    event_payload["ticket_categories"][0]["price_amount"] = price_amount
    response = client.post(MY_EVENTS, json=event_payload, headers=organiser_headers)
    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


def test_validation_details_name_each_field(client, organiser_headers, event_payload):
    event_payload["title"] = " "
    event_payload["ticket_release_at"] = event_payload["starts_at"]
    response = client.post(MY_EVENTS, json=event_payload, headers=organiser_headers)
    details = response.get_json()["error"]["details"]
    assert set(details) == {"title", "ticket_release_at"}


@pytest.mark.parametrize("field", ["starts_at", "ticket_release_at"])
def test_times_must_be_in_the_future(client, organiser_headers, event_payload, field):
    event_payload[field] = iso(datetime.now(UTC) - timedelta(hours=1))
    response = client.post(MY_EVENTS, json=event_payload, headers=organiser_headers)
    assert response.status_code == 400
    assert field in response.get_json()["error"]["details"]


def test_times_need_a_utc_offset(client, organiser_headers, event_payload):
    event_payload["starts_at"] = event_payload["starts_at"].rstrip("Z")
    response = client.post(MY_EVENTS, json=event_payload, headers=organiser_headers)
    assert response.status_code == 400


def test_category_names_are_unique_ignoring_case(client, organiser_headers, event_payload):
    event_payload["ticket_categories"].append(
        {"name": "cat 1", "price_amount": 5000, "total_quantity": 10}
    )
    response = client.post(MY_EVENTS, json=event_payload, headers=organiser_headers)
    assert response.status_code == 400


def test_unknown_fields_are_rejected(client, organiser_headers, event_payload):
    event_payload["ticket_categories"][0]["price"] = "88.00"
    response = client.post(MY_EVENTS, json=event_payload, headers=organiser_headers)
    assert response.status_code == 400


def test_body_must_be_a_json_object(client, organiser_headers):
    response = client.post(MY_EVENTS, data="not json", headers=organiser_headers)
    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


@pytest.mark.parametrize("query", ["page=0", "page=abc", "page_size=101", "page_size=0"])
def test_invalid_pagination_is_rejected(client, query):
    response = client.get(f"{EVENTS}?{query}")
    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


# Responses


def test_organiser_view(client, organiser_headers, event_payload, release_at):
    event = create_draft(client, organiser_headers, event_payload)
    assert event["status"] == "DRAFT"
    assert event["organiser_id"] == organiser_headers["X-User-Sub"]
    assert event["queue_join_opens_at"] == iso(release_at - timedelta(minutes=15))
    assert event["published_at"] is None
    assert event["cancelled_at"] is None
    assert event["cancellation_reason"] is None
    assert event["ticket_categories"][0]["price_amount"] == 8800


def test_public_view_has_only_public_fields(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    publish(client, organiser_headers, event["event_id"])
    public = client.get(f"{EVENTS}/{event['event_id']}").get_json()
    assert set(public) == {
        "event_id",
        "title",
        "description",
        "venue",
        "category",
        "starts_at",
        "ticket_release_at",
        "queue_join_opens_at",
        "status",
        "image_url",
        "ticket_categories",
    }


def test_categories_keep_their_order(client, organiser_headers, event_payload):
    event_payload["ticket_categories"] = [
        {"name": name, "price_amount": 1000, "total_quantity": 10} for name in ["VIP", "CAT 1", "A"]
    ]
    event = create_draft(client, organiser_headers, event_payload)
    assert [c["name"] for c in event["ticket_categories"]] == ["VIP", "CAT 1", "A"]
    assert event["ticket_categories"][0]["currency"] == "SGD"


# Public routes


def test_only_published_events_are_listed(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    assert client.get(EVENTS).get_json()["items"] == []
    assert client.get(f"{EVENTS}/{event['event_id']}").status_code == 404

    publish(client, organiser_headers, event["event_id"])
    listing = client.get(f"{EVENTS}?search=MIDNIGHT&page_size=5").get_json()
    assert listing["page"] == 1
    assert listing["page_size"] == 5
    assert listing["total_items"] == 1
    assert listing["items"][0]["event_id"] == event["event_id"]


def test_search_treats_wildcards_literally(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    publish(client, organiser_headers, event["event_id"])
    assert client.get(f"{EVENTS}?search=%25").get_json()["total_items"] == 0
    assert client.get(f"{EVENTS}?search=_").get_json()["total_items"] == 0


def test_cancelled_event_can_be_opened_but_is_not_listed(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    publish(client, organiser_headers, event["event_id"])
    cancel(client, organiser_headers, event["event_id"])
    assert client.get(EVENTS).get_json()["items"] == []
    assert client.get(f"{EVENTS}/{event['event_id']}").get_json()["status"] == "CANCELLED"


# Organiser routes


def test_list_my_events_newest_first(client, organiser_headers, event_payload, session_factory):
    first = create_draft(client, organiser_headers, event_payload)
    second = create_draft(client, organiser_headers, event_payload)
    create_draft(client, {**organiser_headers, "X-User-Sub": OTHER_ORGANISER}, event_payload)
    with session_factory() as session, session.begin():
        session.execute(
            update(Event)
            .where(Event.event_id == first["event_id"])
            .values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        )

    listing = client.get(MY_EVENTS, headers=organiser_headers).get_json()
    assert [e["event_id"] for e in listing["items"]] == [second["event_id"], first["event_id"]]
    published = client.get(f"{MY_EVENTS}?status=PUBLISHED", headers=organiser_headers).get_json()
    assert published["items"] == []
    assert client.get(f"{MY_EVENTS}?status=nope", headers=organiser_headers).status_code == 400


def test_get_my_event_includes_drafts(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    response = client.get(f"{MY_EVENTS}/{event['event_id']}", headers=organiser_headers)
    assert response.get_json()["status"] == "DRAFT"


def test_update_replaces_the_draft(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    event_payload["title"] = "New title"
    event_payload["ticket_categories"][0]["price_amount"] = 9900
    response = client.put(
        f"{MY_EVENTS}/{event['event_id']}", json=event_payload, headers=organiser_headers
    )
    assert response.status_code == 200
    updated = response.get_json()
    assert updated["title"] == "New title"
    assert updated["ticket_categories"][0]["price_amount"] == 9900
    old_id = event["ticket_categories"][0]["category_id"]
    assert updated["ticket_categories"][0]["category_id"] != old_id


# Event category


def test_category_is_saved_and_shown_to_everyone(client, organiser_headers, event_payload):
    draft = create_draft(client, organiser_headers, {**event_payload, "category": "Concerts"})
    assert draft["category"] == "Concerts"
    assert publish(client, organiser_headers, draft["event_id"]).status_code == 200
    public = client.get(f"{EVENTS}/{draft['event_id']}").get_json()
    assert public["category"] == "Concerts"
    listed = client.get(EVENTS).get_json()["items"]
    assert [item["category"] for item in listed] == ["Concerts"]


def test_category_is_optional(client, organiser_headers, event_payload):
    assert create_draft(client, organiser_headers, event_payload)["category"] is None


def test_an_unknown_category_is_rejected(client, organiser_headers, event_payload):
    response = client.post(
        MY_EVENTS, json={**event_payload, "category": "Karaoke"}, headers=organiser_headers
    )
    assert response.status_code == 400
    assert error_code(response) == "VALIDATION_ERROR"


@pytest.mark.parametrize("category", ["Concerts", "Theatre", "Festivals", "Sports", "Conferences"])
def test_every_listed_category_is_accepted(client, organiser_headers, event_payload, category):
    draft = create_draft(client, organiser_headers, {**event_payload, "category": category})
    assert draft["category"] == category


def test_updating_a_draft_changes_its_category(client, organiser_headers, event_payload):
    draft = create_draft(client, organiser_headers, {**event_payload, "category": "Concerts"})
    response = client.put(
        f"{MY_EVENTS}/{draft['event_id']}",
        json={**event_payload, "category": "Sports"},
        headers=organiser_headers,
    )
    assert response.status_code == 200
    assert response.get_json()["category"] == "Sports"


def test_category_is_not_added_to_the_published_message(
    client, organiser_headers, event_payload, session_factory
):
    draft = create_draft(client, organiser_headers, {**event_payload, "category": "Concerts"})
    publish(client, organiser_headers, draft["event_id"])
    payload = outbox(session_factory, "EventPublished")[0].payload["payload"]
    assert "category" not in payload


def test_update_validates_the_whole_body(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    response = client.put(
        f"{MY_EVENTS}/{event['event_id']}", json={"title": "x"}, headers=organiser_headers
    )
    assert response.status_code == 400


def test_only_drafts_can_be_updated(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    publish(client, organiser_headers, event["event_id"])
    response = client.put(
        f"{MY_EVENTS}/{event['event_id']}", json=event_payload, headers=organiser_headers
    )
    assert response.status_code == 409
    assert error_code(response) == "INVALID_EVENT_STATE"


# Publishing


def test_publish_saves_the_event_and_one_message(
    client, organiser_headers, event_payload, session_factory
):
    event = create_draft(client, organiser_headers, event_payload)
    response = publish(client, organiser_headers, event["event_id"])
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "PUBLISHED"
    assert body["published_at"] is not None

    [message] = outbox(session_factory)
    assert message.message_type == "EventPublished"
    assert message.payload["payload"]["event_id"] == event["event_id"]

    repeat = publish(client, organiser_headers, event["event_id"])
    assert repeat.status_code == 409
    assert len(outbox(session_factory)) == 1


def test_message_carries_the_correlation_id(
    client, organiser_headers, event_payload, session_factory
):
    event = create_draft(client, organiser_headers, event_payload)
    client.post(
        f"{MY_EVENTS}/{event['event_id']}/publish",
        headers={**organiser_headers, "X-Correlation-Id": "trace-2"},
    )
    assert outbox(session_factory)[0].correlation_id == "trace-2"


@pytest.mark.parametrize(
    "release_minutes, rejected",
    [(10, True), (-5, True), (20, False)],
)
def test_publish_needs_release_at_least_15_minutes_away(
    client, organiser_headers, event_payload, release_minutes, rejected
):
    session_factory = client.application.extensions["session_factory"]
    event = create_draft(client, organiser_headers, event_payload)
    now = datetime.now(UTC)
    with session_factory() as session, session.begin():
        session.execute(
            update(Event)
            .where(Event.event_id == event["event_id"])
            .values(
                ticket_release_at=now + timedelta(minutes=release_minutes),
                starts_at=now + timedelta(hours=1),
            )
        )
    response = publish(client, organiser_headers, event["event_id"])
    if not rejected:
        assert response.status_code == 200
        return
    assert response.status_code == 400
    assert "ticket_release_at" in response.get_json()["error"]["details"]
    assert not outbox(session_factory)


# Cancelling


def test_cancel_saves_the_reason_and_one_message(
    client, organiser_headers, event_payload, session_factory
):
    event = create_draft(client, organiser_headers, event_payload)
    publish(client, organiser_headers, event["event_id"])
    response = cancel(client, organiser_headers, event["event_id"])
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "CANCELLED"
    assert body["cancellation_reason"] == "Venue closure"
    assert body["cancelled_at"] is not None

    [message] = outbox(session_factory, "EventCancelled")
    assert message.payload["payload"]["cancelled_at"] == body["cancelled_at"]

    assert cancel(client, organiser_headers, event["event_id"]).status_code == 409
    assert len(outbox(session_factory, "EventCancelled")) == 1


def test_cancel_needs_a_reason(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    publish(client, organiser_headers, event["event_id"])
    response = cancel(client, organiser_headers, event["event_id"], reason=" ")
    assert response.status_code == 400


def test_drafts_cannot_be_cancelled(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    assert cancel(client, organiser_headers, event["event_id"]).status_code == 409


# Completion


def test_events_complete_after_they_start(
    client, organiser_headers, event_payload, session_factory
):
    event = create_draft(client, organiser_headers, event_payload)
    publish(client, organiser_headers, event["event_id"])
    draft = create_draft(client, organiser_headers, event_payload)
    later = datetime.now(UTC) + timedelta(days=60)

    assert complete_past_events(session_factory) == 0
    assert complete_past_events(session_factory, now=later) == 1

    completed = client.get(f"{EVENTS}/{event['event_id']}").get_json()
    assert completed["status"] == "COMPLETED"
    mine = client.get(f"{MY_EVENTS}/{draft['event_id']}", headers=organiser_headers).get_json()
    assert mine["status"] == "DRAFT"
    assert cancel(client, organiser_headers, event["event_id"]).status_code == 409


# Event versions


def test_every_change_raises_the_version(client, organiser_headers, event_payload, session_factory):
    event = create_draft(client, organiser_headers, event_payload)
    client.put(f"{MY_EVENTS}/{event['event_id']}", json=event_payload, headers=organiser_headers)
    publish(client, organiser_headers, event["event_id"])
    cancel(client, organiser_headers, event["event_id"])

    published, cancelled = outbox(session_factory)
    assert published.payload["payload"]["event_version"] == 3
    assert cancelled.payload["payload"]["event_version"] == 4


# Internal API


def test_category_lookup(client, organiser_headers, event_payload):
    event = create_draft(client, organiser_headers, event_payload)
    category_id = event["ticket_categories"][0]["category_id"]
    url = f"/internal/v1/events/{event['event_id']}/categories/{category_id}"
    assert client.get(url).status_code == 404

    publish(client, organiser_headers, event["event_id"])
    assert client.get(url).get_json() == {
        "event_id": event["event_id"],
        "event_status": "PUBLISHED",
        "category_id": category_id,
        "name": "CAT 1",
        "price_amount": 8800,
        "currency": "SGD",
        "total_quantity": 500,
    }

    missing = client.get(f"/internal/v1/events/{event['event_id']}/categories/nope")
    assert missing.status_code == 404
    assert error_code(missing) == "EVENT_NOT_FOUND"


@pytest.mark.parametrize(
    "groups",
    [
        "[organiser]",
        "organiser",
        "[admin organiser]",
        "admin organiser",
        "admin,organiser",
        '["admin", "organiser"]',
    ],
)
def test_an_organiser_in_several_groups_can_create_events(
    client, organiser_headers, event_payload, groups
):
    headers = {**organiser_headers, "X-User-Groups": groups}
    response = client.post("/api/v1/organizer/events", json=event_payload, headers=headers)
    assert response.status_code == 201
