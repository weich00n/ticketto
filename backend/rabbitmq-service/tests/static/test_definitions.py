"""The generated definitions say what the plan says, without starting a broker."""

import copy
import json
import re

import pytest
import yaml

from tools import generate_definitions as gen

CATALOGUE = gen.load_catalogue()
DEFINITIONS = json.loads(gen.DEFINITIONS.read_text(encoding="utf-8"))
QUEUES = {q["name"]: q for q in DEFINITIONS["queues"]}
EXCHANGES = {e["name"]: e for e in DEFINITIONS["exchanges"]}
POLICIES = {p["name"]: p for p in DEFINITIONS["policies"]}
CONSUMER_QUEUES = [q.name for q in CATALOGUE.consumer_queues()]
PUBLISHER_EXCHANGES = ["checkout.events", "event.events", "reservation.events", "ticket.events"]


def test_committed_outputs_match_the_catalogue():
    for path, text in gen.render(CATALOGUE).items():
        assert path.read_text(encoding="utf-8") == text, (
            f"{path.name} is stale: run `python -m tools.generate_definitions`"
        )


@pytest.mark.parametrize("name", PUBLISHER_EXCHANGES)
def test_publisher_exchanges_match_how_services_declare_them(name):
    # event-service/app/outbox.py and queue-service/app/messaging/topology.py both do
    # exchange_declare(name, exchange_type="topic", durable=True)
    assert EXCHANGES[name] == {
        "name": name,
        "vhost": "/",
        "type": "topic",
        "durable": True,
        "auto_delete": False,
        "internal": False,
        "arguments": {},
    }


def test_exchanges():
    assert {name: e["type"] for name, e in EXCHANGES.items()} == {
        **dict.fromkeys(PUBLISHER_EXCHANGES, "topic"),
        "ticketto.dlx": "direct",
        "ticketto.unrouted": "fanout",
    }
    assert all(e["durable"] and not e["auto_delete"] for e in EXCHANGES.values())


def test_queue_counts():
    assert len(CONSUMER_QUEUES) == 20
    assert len(QUEUES) == 2 * len(CONSUMER_QUEUES) + 1  # each with a DLQ, plus unrouted


def test_every_queue_is_durable_quorum():
    for queue in QUEUES.values():
        assert queue["durable"] is True and queue["auto_delete"] is False, queue["name"]
        assert queue["arguments"]["x-queue-type"] == "quorum", queue["name"]


@pytest.mark.parametrize("name", CONSUMER_QUEUES)
def test_consumer_queue_dead_letters_to_its_own_dlq(name):
    assert QUEUES[name]["arguments"] == {
        "x-queue-type": "quorum",
        "x-dead-letter-exchange": "ticketto.dlx",
        "x-dead-letter-routing-key": name,
        "x-dead-letter-strategy": "at-least-once",
        "x-overflow": "reject-publish",
    }
    assert f"{name}.dlq" in QUEUES
    assert {
        "source": "ticketto.dlx",
        "vhost": "/",
        "destination": f"{name}.dlq",
        "destination_type": "queue",
        "routing_key": name,
        "arguments": {},
    } in DEFINITIONS["bindings"]


def test_each_publisher_publishes_to_its_own_exchange():
    assert CATALOGUE.message("EventPublished").exchange == "event.events"
    assert CATALOGUE.message("OrderConfirmed").exchange == "checkout.events"
    assert CATALOGUE.message("EventSoldOut").exchange == "reservation.events"


def test_bindings_follow_the_catalogue_exactly():
    publisher_bindings = {
        (b["source"], b["routing_key"], b["destination"])
        for b in DEFINITIONS["bindings"]
        if b["source"] in PUBLISHER_EXCHANGES
    }
    expected = {
        (q.message.exchange, q.message.routing_key, q.name) for q in CATALOGUE.consumer_queues()
    }
    assert publisher_bindings == expected


def test_bindings_point_at_things_that_exist_and_use_no_wildcards():
    for binding in DEFINITIONS["bindings"]:
        assert binding["source"] in EXCHANGES
        assert binding["destination"] in QUEUES
        assert "*" not in binding["routing_key"] and "#" not in binding["routing_key"]


def test_unrouted_queue_catches_everything_sent_to_the_alternate_exchange():
    assert {
        "source": "ticketto.unrouted",
        "vhost": "/",
        "destination": "ticketto.unrouted",
        "destination_type": "queue",
        "routing_key": "",
        "arguments": {},
    } in DEFINITIONS["bindings"]
    assert POLICIES["events-ae"]["definition"] == {"alternate-exchange": "ticketto.unrouted"}
    assert POLICIES["events-ae"]["apply-to"] == "exchanges"


@pytest.mark.parametrize("name", sorted(EXCHANGES))
def test_only_publisher_exchanges_get_the_alternate_exchange(name):
    # ticketto.dlx must not: a dead letter that matches no DLQ is a bug, not something to keep.
    assert _policy_matches("events-ae", name) is (name in PUBLISHER_EXCHANGES)


def _policy_matches(policy: str, name: str) -> bool:
    return re.search(POLICIES[policy]["pattern"], name) is not None


@pytest.mark.parametrize("name", sorted(QUEUES))
def test_every_queue_gets_exactly_the_policy_meant_for_it(name):
    is_consumer_queue = name in CONSUMER_QUEUES
    assert _policy_matches("consumer-queues", name) is is_consumer_queue
    assert _policy_matches("holding-cap", name) is not is_consumer_queue
    assert not _policy_matches("events-ae", name)


def test_policy_settings_come_from_the_catalogue():
    assert POLICIES["consumer-queues"]["definition"] == {"delivery-limit": 5}
    assert POLICIES["holding-cap"]["definition"] == {
        "max-length": 50000,
        "overflow": "reject-publish",  # refuse, never silently drop
        "delivery-limit": -1,  # never drop after repeated failed deliveries
    }


def test_vhost_defaults_to_quorum_queues():
    assert DEFINITIONS["vhosts"][0]["metadata"]["default_queue_type"] == "quorum"


def test_no_users_or_secrets_in_git():
    assert "users" not in DEFINITIONS and "permissions" not in DEFINITIONS
    assert "password" not in gen.DEFINITIONS.read_text(encoding="utf-8")


def test_queue_list_matches_definitions():
    lines = gen.QUEUE_LIST.read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if not line.startswith("#")] == sorted(QUEUES)


# ---------- catalogue validation ----------

RAW = yaml.safe_load(gen.CATALOGUE.read_text(encoding="utf-8"))


def _broken(mutate) -> dict:
    raw = copy.deepcopy(RAW)
    mutate(raw)
    return raw


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda r: r["messages"].update(event_published=r["messages"]["EventPublished"]),
            "PascalCase",
        ),
        (lambda r: r["messages"]["EventPublished"].update(consumers=["queue"]), "service name"),
        (
            lambda r: r["messages"]["EventPublished"].update(consumers=["a-service", "a-service"]),
            "twice",
        ),
        (lambda r: r["messages"]["EventPublished"].update(status="done"), "status"),
        (lambda r: r["messages"]["EventPublished"].pop("consumers"), "missing consumers"),
        (lambda r: r["messages"]["EventPublished"].update(queue="x"), "unknown key"),
        (lambda r: r["messages"]["EventPublished"].pop("routing_key"), "missing routing_key"),
        (
            lambda r: r["messages"]["EventPublished"].update(routing_key="EventPublished"),
            "dotted lower case",
        ),
        (lambda r: r["messages"]["EventPublished"].update(routing_key="event.*"), "dotted"),
        (
            lambda r: r["messages"]["EventPublished"].update(routing_key="event.cancelled"),
            "already used by EventPublished",
        ),
        (lambda r: r["broker"].update(delivery_limit=0), "positive integer"),
    ],
)
def test_invalid_catalogues_are_rejected(mutate, error):
    with pytest.raises(gen.CatalogueError, match=error):
        gen.parse_catalogue(_broken(mutate))


def test_duplicate_message_names_are_rejected(tmp_path):
    path = tmp_path / "messages.yaml"
    duplicate = "\n".join(
        [
            "  EventPublished:",
            "    publisher: event-service",
            "    routing_key: event.republished",
            "    status: planned",
            "    consumers: []",
            "",
        ]
    )
    path.write_text(gen.CATALOGUE.read_text(encoding="utf-8") + duplicate, encoding="utf-8")
    with pytest.raises(gen.CatalogueError, match="duplicate key 'EventPublished'"):
        gen.load_catalogue(path)


def test_check_mode_reports_stale_output(tmp_path, monkeypatch, capsys):
    stale = tmp_path / "definitions.json"
    stale.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(gen, "DEFINITIONS", stale)
    monkeypatch.setattr(gen, "QUEUE_LIST", tmp_path / "queues.txt")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    assert gen.main(["--check"]) == 1
    assert "Out of date" in capsys.readouterr().err
