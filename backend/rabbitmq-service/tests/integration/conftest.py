"""The integration suite runs the real broker image in Docker, started with Testcontainers.

Image under test:
  $BROKER_IMAGE if set: CI passes the exact image built earlier in the pipeline.
  Otherwise this repo's Dockerfile is built as ticketto-rabbitmq:test.

Run with: pytest -m integration   (Docker must be running)
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Iterator
from urllib.parse import quote

import pika
import pytest
import requests
from testcontainers.core.container import DockerContainer

from tools import generate_definitions as gen

ADMIN_USER = "ticketto"
ADMIN_PASS = "integration-tests-only"
# Service users: one has its own password, as on AWS; the rest use the local-style default.
SERVICE_PASS_DEFAULT = "service-tests-only"
RESERVATION_PASS = "reservation-tests-only"
LOCAL_IMAGE = "ticketto-rabbitmq:test"

CATALOGUE = gen.load_catalogue()
QUEUE_NAMES = gen.queue_names(CATALOGUE)
CONSUMER_QUEUES = [q.name for q in CATALOGUE.consumer_queues()]


def wait_for(condition: Callable[[], object], *, timeout: float = 10, what: str = "condition"):
    """Poll until condition() is truthy, and return its value."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = condition()
            if value:
                return value
        except Exception:  # pylint: disable=broad-exception-caught  # retried until the deadline
            if time.monotonic() >= deadline:
                raise
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(0.25)


class Broker:
    """One running broker container and convenient ways to talk to it."""

    def __init__(self, container: DockerContainer, password: str = ADMIN_PASS):
        self.container = container
        self.password = password

    # ---- addresses (re-read every time: ports can change when the container restarts) ----

    def amqp_url(self, user: str = ADMIN_USER, password: str | None = None) -> str:
        host = self.container.get_container_host_ip()
        port = self.container.get_exposed_port(5672)
        return f"amqp://{user}:{password or self.password}@{host}:{port}/%2F"

    def api_url(self, path: str) -> str:
        host = self.container.get_container_host_ip()
        return f"http://{host}:{self.container.get_exposed_port(15672)}/api/{path}"

    # ---- clients ----

    def connect(self, user: str = ADMIN_USER, password: str | None = None):
        params = pika.URLParameters(self.amqp_url(user, password))
        params.connection_attempts = 1
        return pika.BlockingConnection(params)

    def api(self, method: str, path: str, **kwargs) -> requests.Response:
        return requests.request(
            method, self.api_url(path), auth=(ADMIN_USER, self.password), timeout=10, **kwargs
        )

    def api_json(self, path: str):
        response = self.api("GET", path)
        response.raise_for_status()
        return response.json()

    def exec(self, *command: str) -> tuple[int, str]:
        result = self.container.get_wrapped_container().exec_run(list(command))
        return result.exit_code, result.output.decode()

    # ---- lifecycle ----

    def wait_until_ready(self, timeout: float = 120) -> None:
        """Ready = AMQP accepts our login, the topology is loaded and the API answers."""

        def ready() -> bool:
            with self.connect() as connection:
                connection.channel().queue_declare(gen.UNROUTED, passive=True)
            return self.api("GET", "overview").status_code == 200

        wait_for(ready, timeout=timeout, what="the broker to be ready")

    def restart(self) -> None:
        self.container.get_wrapped_container().restart()
        self.container.get_wrapped_container().reload()
        self.wait_until_ready()


def start_broker(image: str, *, password: str = ADMIN_PASS, volume: str | None = None) -> Broker:
    container = (
        DockerContainer(image)
        .with_env("RABBITMQ_ADMIN_USER", ADMIN_USER)
        .with_env("RABBITMQ_ADMIN_PASS", password)
        .with_env("RABBITMQ_SERVICE_PASS_DEFAULT", SERVICE_PASS_DEFAULT)
        .with_env("RABBITMQ_PASS_RESERVATION_SERVICE", RESERVATION_PASS)
        .with_exposed_ports(5672, 15672)
        # Same node name as on the EC2 host; quorum queue data is stored under it.
        .with_kwargs(hostname="broker")
    )
    if volume:
        container = container.with_volume_mapping(volume, "/var/lib/rabbitmq", "rw")
    container.start()
    broker = Broker(container, password)
    try:
        broker.wait_until_ready()
    except Exception:
        print(container.get_wrapped_container().logs().decode()[-5000:])
        container.stop()
        raise
    return broker


@pytest.fixture(scope="session")
def broker_image() -> str:
    image = os.environ.get("BROKER_IMAGE")
    if image:
        return image
    # The Docker CLI, not the Python SDK: the Dockerfile needs BuildKit (COPY --chmod).
    subprocess.run(["docker", "build", "-q", "-t", LOCAL_IMAGE, str(gen.ROOT)], check=True)
    return LOCAL_IMAGE


@pytest.fixture(scope="session")
def broker(broker_image) -> Iterator[Broker]:
    started = start_broker(broker_image)
    yield started
    started.container.stop()


@pytest.fixture()
def channel(broker):
    """A channel with publisher confirms on, as every Ticketto publisher must use."""
    with broker.connect() as connection:
        ch = connection.channel()
        ch.confirm_delivery()
        yield ch


@pytest.fixture()
def empty_queues(broker):
    """Every catalogue queue starts (and ends) the test empty."""

    def purge():
        with broker.connect() as connection:
            ch = connection.channel()
            for name in QUEUE_NAMES:
                ch.queue_purge(name)

    purge()
    yield
    purge()


def publish(  # pylint: disable=too-many-arguments  # keyword-only options
    ch,
    message: str,
    body: bytes = b"{}",
    *,
    exchange: str | None = None,
    routing_key: str | None = None,
    persistent: bool = True,
    message_id: str = "00000000-0000-4000-8000-000000000000",
) -> None:
    """Publish the way docs/MESSAGING.md tells publishers to: to the publisher's exchange
    with the message's routing key, persistent, mandatory, with message_id/type/correlation_id
    set. Raises if the broker nacks or returns the message.

    `message` is a catalogue name like "EventPublished"; exchange and routing_key default to
    its catalogue entry and can be overridden to send it somewhere it shouldn't go."""
    if exchange is None or routing_key is None:
        entry = CATALOGUE.message(message)
        exchange = entry.exchange if exchange is None else exchange
        routing_key = entry.routing_key if routing_key is None else routing_key
    ch.basic_publish(
        exchange=exchange,
        routing_key=routing_key,
        body=body,
        properties=pika.BasicProperties(
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent
            if persistent
            else pika.DeliveryMode.Transient,
            message_id=message_id,
            correlation_id="corr-1",
            type=message,
        ),
        mandatory=True,
    )


def message_counts(ch) -> dict[str, int]:
    """Ready messages in every catalogue queue (passive declare: creates and changes nothing)."""
    return {name: ch.queue_declare(name, passive=True).method.message_count for name in QUEUE_NAMES}


def queue_path(name: str) -> str:
    return f"queues/%2F/{quote(name, safe='')}"
