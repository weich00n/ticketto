# ticketto-contracts (Python)

Ticketto's AMQP message contracts, and the contract tests a service runs against them in its own
test suite. No broker and no other services are needed: the tests run in seconds.

`ticketto-contracts X.Y.Z` contains contracts `X.Y.Z` from rabbitmq-service: the message catalogue
(`topology/messages.yaml`), the envelope and payload schemas, examples and consumer requirements
(`contracts/`). See [the reference page](../../docs/AMQP_Message_Contracts.md) and
[the changelog](../../contracts/CHANGELOG.md).

## Install

It is published to rabbitmq-service's GitLab PyPI registry. Add it as a test dependency, pinned:

```toml
# pyproject.toml
[project.optional-dependencies]
dev = ["pytest", "ticketto-contracts==1.0.0"]
```

- **CI:** `pip install --index-url "https://gitlab-ci-token:${CI_JOB_TOKEN}@gitlab.com/api/v4/projects/<rabbitmq-service project id>/packages/pypi/simple" ...`.
  rabbitmq-service must allow your project under **Settings → CI/CD → Job token permissions**.
- **Your machine:** a GitLab personal access token with `read_api`, in `~/.config/pip/pip.conf`:

  ```ini
  [global]
  extra-index-url = https://__token__:<token>@gitlab.com/api/v4/projects/<rabbitmq-service project id>/packages/pypi/simple
  ```

## Add the contract tests

One test module; its name and folder are up to you:

```python
# tests/contract/test_contracts.py
from ticketto_contracts.suite import *  # noqa: F403

CONTRACT_SERVICE = "checkout-service"
CONTRACT_BODIES = []  # optional: ["extra-fields", "invalid"]
```

Then fixtures in `conftest.py` for the roles the catalogue gives your service. The package
generates the tests from the catalogue, so you write adapters, not tests.

### If your service publishes

```python
@pytest.fixture
def contract_publish():
    def publish(message_name, channel):
        # Make your real outbox relay publish one `message_name` on `channel`, a
        # ticketto_contracts.RecordingChannel that stands in for a pika BlockingChannel.
        row = make_outbox_row_for_test(message_name)  # through the code that writes real rows
        relay_once(row, channel)

    return publish
```

`test_publishes[<Message>]` runs once per message you publish and checks everything the relay
handed to the channel: exchange, routing key, `mandatory`, publisher confirms, the AMQP properties
(`delivery_mode=2`, `message_id`, `type`, `correlation_id`, `content_type`), the envelope, and the
payload against your schema. It also fails on a `queue_declare` without `passive=True`, or an
`exchange_declare` that differs from the one in docs/MESSAGING.md.

### If your service consumes

```python
@pytest.fixture
def contract_queues():
    return {"checkout-service.EventCancelled", "checkout-service.ReservationExpired", ...}


@pytest.fixture
def contract_handle(db_session):  # whatever your handler needs, stubbed just enough
    def handle(queue, body, properties):
        # Run your real handler for `queue` on this body and AMQP properties (a dict),
        # and report what it did: "ack", "reject" (without requeue) or "requeue".
        ...

    return handle
```

- `test_listens_on_catalogued_queues`: your listeners are exactly your catalogued queues, with
  none missing and none extra (no DLQs, no queues of your own).
- `test_accepts[<Message>-minimal]`: your handler acks a body with only the fields the publisher
  always sends. If it needs a field the publisher's schema doesn't require, it fails here: add
  the field to your `contracts/requirements/<service>.yaml` in rabbitmq-service, and the
  publisher must agree to always send it.
- With `CONTRACT_BODIES`: `extra-fields` (ack a body with unknown fields; you must ignore them)
  and `invalid` (reject, without requeue, a body missing a required field).

There is no way to mark a test as an expected failure: if your service doesn't match the
contracts, its tests fail until the service or the contract is fixed. (`CONTRACT_KNOWN_GAPS`
existed until 1.0.0; from 2.0.0 the package refuses it.)

## Use the pieces directly

```python
from ticketto_contracts import catalogue, assert_valid_publish, PublishedMessage, bodies

catalogue.published_by("checkout-service")  # ["OrderConfirmed", ...]
catalogue.queues_for("ticket-service")  # ["ticket-service.EventPublished", ...]
assert_valid_publish(
    "checkout-service",
    PublishedMessage(
        exchange=..., routing_key=..., body=..., properties={...}, mandatory=True, confirmed=True
    ),
)
bodies.delivery("OrderConfirmed", "minimal")  # (body bytes, properties) as a good publisher sends
```

A `ContractError` lists every problem with a code (`routing_key`, `payload`, ...). The codes are
shared with the Java package: [packages/conformance/cases.json](../conformance/cases.json).

## Develop this package (in rabbitmq-service)

```sh
python -m tools.build_contracts_bundle --out packages/python/src/ticketto_contracts/data
pip install -e packages/python
pytest packages/python/tests      # rebuilds the bundle from this checkout first
```

CI (`contracts-python`) builds the wheel, installs it and runs these tests against it; on `main`,
`publish-contracts-python` uploads that wheel when `contracts/VERSION` changes.
