"""notification-service's side of the AMQP message contracts, checked with rabbitmq-service's
ticketto-contracts package (pinned in requirements-contract.txt).

The tests are generated from the contract catalogue (see ticketto_contracts.suite):
- test_listens_on_catalogued_queues: each run drains exactly notification-service's queues
- test_accepts[<Message>-minimal]: notification-service acks a body with only the fields the
  publisher always sends, so it never depends on a field it might not get
- test_accepts[<Message>-extra-fields]: it ignores fields it doesn't know
- test_accepts[<Message>-invalid]: it dead-letters a body missing a required field
notification-service publishes nothing, so test_publishes is skipped.

Run them as described in README.md ("Contract tests"). Without the package they are skipped.
"""

import pytest

pytest.importorskip("ticketto_contracts", reason="needs ticketto-contracts: see README.md")

# The generated tests and their pytest hook. Wildcard on purpose: the package decides which
# tests exist, from the catalogue.
from ticketto_contracts.suite import *  # noqa: E402, F403  # pylint: disable=wildcard-import,unused-wildcard-import,wrong-import-position

pytestmark = pytest.mark.contract

CONTRACT_SERVICE = "notification-service"
CONTRACT_BODIES = ["extra-fields", "invalid"]
