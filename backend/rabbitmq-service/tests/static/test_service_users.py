"""Each service's broker user can reach exactly what messages.yaml gives that service: its
own consumer queues (not their DLQs), its own exchange, and its own messages' routing keys."""

import json
import re

import pytest

from tools import generate_definitions as gen

CATALOGUE = gen.load_catalogue()
DEFINITIONS = json.loads(gen.DEFINITIONS.read_text(encoding="utf-8"))
QUEUES = [q["name"] for q in DEFINITIONS["queues"]]
EXCHANGES = [e["name"] for e in DEFINITIONS["exchanges"]]
ROUTING_KEYS = [m.routing_key for m in CATALOGUE.messages]
USERS = {u.name: u for u in gen.service_users(CATALOGUE)}
SERVICES = sorted(
    {m.publisher for m in CATALOGUE.messages} | {c for m in CATALOGUE.messages for c in m.consumers}
)


def _matching(pattern: str, names: list[str]) -> set[str]:
    # RabbitMQ matches permission regexes like re.search; ours are anchored with ^...$.
    return {n for n in names if re.search(pattern, n)}


def test_every_service_has_exactly_one_user():
    assert sorted(USERS) == SERVICES


@pytest.mark.parametrize("service", SERVICES)
def test_reads_exactly_its_own_consumer_queues_and_not_their_dlqs(service):
    own = {q.name for q in CATALOGUE.consumer_queues() if q.name.startswith(f"{service}.")}
    assert _matching(USERS[service].read, QUEUES) == own


@pytest.mark.parametrize("service", SERVICES)
def test_writes_and_configures_only_its_own_exchange(service):
    publishes = any(m.publisher == service for m in CATALOGUE.messages)
    own = {gen.publisher_exchange(service)} if publishes else set()
    assert _matching(USERS[service].write, EXCHANGES) == own
    assert _matching(USERS[service].configure, EXCHANGES) == own
    assert not _matching(USERS[service].configure, QUEUES)  # can't declare or delete queues


@pytest.mark.parametrize("service", SERVICES)
def test_publishes_only_its_own_routing_keys(service):
    own = {m.routing_key for m in CATALOGUE.messages if m.publisher == service}
    user = USERS[service]
    assert user.topic_exchange == (gen.publisher_exchange(service) if own else None)
    assert _matching(user.topic_write, ROUTING_KEYS) == own
    # Dots are literal: "event.published" must not match "eventXpublished".
    for key in own:
        assert not re.search(user.topic_write, key.replace(".", "X"))


def test_a_consumer_only_service_can_publish_nothing():
    assert USERS["queue-service"].write == gen.NOTHING
    assert USERS["queue-service"].topic_exchange is None


def test_a_publisher_only_service_can_read_nothing():
    assert USERS["event-service"].read == gen.NOTHING


def test_users_file_matches_the_users_and_is_json_escaped():
    rows = [
        line.split("\t")
        for line in gen.SERVICE_USERS.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert [row[0] for row in rows] == sorted(USERS)
    for name, configure, write, read, topic_exchange, topic_write in rows:
        user = USERS[name]
        # The entrypoint pastes each field into a JSON string as is.
        assert json.loads(f'"{configure}"') == user.configure
        assert json.loads(f'"{write}"') == user.write
        assert json.loads(f'"{read}"') == user.read
        assert topic_exchange == (user.topic_exchange or "-")
        assert json.loads(f'"{topic_write}"') == user.topic_write


def test_no_service_user_name_needs_json_escaping():
    for name in USERS:
        assert re.fullmatch(r"[a-z]+(-[a-z]+)*", name)
