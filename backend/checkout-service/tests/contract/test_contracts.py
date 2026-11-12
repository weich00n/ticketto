"""checkout-service's side of the AMQP message contracts, checked with rabbitmq-service's
ticketto-contracts package (pinned in requirements-contract.txt).

The tests are generated from the contract catalogue (see ticketto_contracts.suite):
- test_publishes[<Message>]: everything the outbox relay sends for each message checkout
  publishes: exchange, routing key, mandatory, publisher confirms, the AMQP properties, the
  envelope and the payload against its schema
- test_listens_on_catalogued_queues: the worker consumes exactly checkout's queues
- test_accepts[<Message>-minimal]: checkout acks a body with only the fields the publisher
  always sends, so it never depends on a field it might not get
- test_accepts[<Message>-extra-fields]: checkout ignores fields it doesn't know
- test_accepts[<Message>-invalid]: checkout dead-letters a body missing a required field

Run them as described in README.md ("Contract tests"). Without the package they are skipped.
"""

import pytest

pytest.importorskip("ticketto_contracts", reason="needs ticketto-contracts: see README.md")

# The generated tests and their pytest hook. Wildcard on purpose: the package decides which
# tests exist, from the catalogue.
from ticketto_contracts.suite import *  # noqa: E402, F403  # pylint: disable=wildcard-import,unused-wildcard-import,wrong-import-position

pytestmark = pytest.mark.contract

CONTRACT_SERVICE = "checkout-service"
CONTRACT_BODIES = ["extra-fields", "invalid"]
