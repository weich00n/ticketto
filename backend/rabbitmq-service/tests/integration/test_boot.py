"""The image boots with the whole topology, the configured limits, and only our admin user."""

import json

import pika
import pytest

from tests.integration.conftest import (
    ADMIN_USER,
    CATALOGUE,
    CONSUMER_QUEUES,
    QUEUE_NAMES,
    queue_path,
    wait_for,
)
from tools import generate_definitions as gen

pytestmark = pytest.mark.integration

DEFINITIONS = json.loads(gen.DEFINITIONS.read_text(encoding="utf-8"))


def test_every_exchange_is_loaded_as_defined(broker):
    loaded = {e["name"]: e for e in broker.api_json("exchanges/%2F")}
    for expected in DEFINITIONS["exchanges"]:
        actual = loaded[expected["name"]]
        for key in ("type", "durable", "auto_delete", "internal", "arguments"):
            assert actual[key] == expected[key], (expected["name"], key)


def test_every_queue_is_loaded_as_a_durable_quorum_queue(broker):
    loaded = {q["name"]: q for q in broker.api_json("queues/%2F")}
    assert set(QUEUE_NAMES) <= set(loaded)
    for expected in DEFINITIONS["queues"]:
        actual = loaded[expected["name"]]
        assert actual["type"] == "quorum", expected["name"]
        assert actual["durable"] is True
        assert actual["arguments"] == expected["arguments"], expected["name"]


def test_every_binding_is_loaded(broker):
    def key(b):
        return (b["source"], b["destination"], b["routing_key"])

    loaded = {key(b) for b in broker.api_json("bindings/%2F") if b["source"]}
    assert {key(b) for b in DEFINITIONS["bindings"]} <= loaded


def test_policies_apply_to_the_right_queues_and_exchange(broker):
    def applied_policy(name):
        return broker.api_json(queue_path(name)).get("policy")

    for name in QUEUE_NAMES:
        expected = "consumer-queues" if name in CONSUMER_QUEUES else "holding-cap"
        assert wait_for(lambda n=name, e=expected: applied_policy(n) == e, what=name)
    for exchange in CATALOGUE.exchanges():
        assert broker.api_json(f"exchanges/%2F/{exchange}")["policy"] == "events-ae", exchange
    assert "policy" not in broker.api_json(f"exchanges/%2F/{gen.DEAD_LETTER_EXCHANGE}")


def test_vhost_defaults_to_quorum(broker):
    assert broker.api_json("vhosts/%2F")["default_queue_type"] == "quorum"


def test_only_the_admin_and_the_service_users_exist(broker):
    services = [u.name for u in gen.service_users(CATALOGUE)]
    users = {u["name"]: u["tags"] for u in broker.api_json("users")}
    assert users == {ADMIN_USER: ["administrator"]} | dict.fromkeys(services, [])


def test_guest_cannot_log_in(broker):
    with pytest.raises(pika.exceptions.ProbableAuthenticationError):
        broker.connect("guest", "guest")


def test_wrong_admin_password_is_rejected(broker):
    with pytest.raises(pika.exceptions.ProbableAuthenticationError):
        broker.connect(ADMIN_USER, "not-the-password")


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("consumer_timeout", "{ok,1800000}"),
        ("vm_memory_high_watermark", '{ok,{absolute,"768MiB"}}'),
        ("disk_free_limit", '{ok,"1GiB"}'),
    ],
)
def test_configuration_is_applied(broker, setting, expected):
    code, output = broker.exec(
        "rabbitmqctl", "-q", "eval", f"application:get_env(rabbit, {setting})."
    )
    assert code == 0
    assert output.strip() == expected


def test_plugins_are_enabled(broker):
    code, output = broker.exec("rabbitmq-plugins", "-q", "list", "--enabled", "--minimal")
    assert code == 0
    assert set(output.split()) >= {
        "rabbitmq_management",
        "rabbitmq_prometheus",
        "rabbitmq_shovel",
        "rabbitmq_shovel_management",
    }


def test_container_reports_healthy(broker):
    container = broker.container.get_wrapped_container()

    def healthy():
        container.reload()
        return container.attrs["State"]["Health"]["Status"] == "healthy"

    assert wait_for(healthy, timeout=90, what="the Docker health check")


def test_ticketto_check_passes_on_a_fresh_broker(broker):
    code, output = broker.exec("ticketto-check")
    assert code == 0, output
    assert f"{len(QUEUE_NAMES)} catalogue queues present" in output
    assert "WARN" not in output, output
