"""Queue's side of the AMQP message contracts, checked with rabbitmq-service's
ticketto-contracts package (pinned in pyproject.toml, `contract` dependency group).

The tests are generated from the contract catalogue (see ticketto_contracts.suite):
- test_listens_on_catalogued_queues: Queue consumes exactly its queues on the shared broker
- test_accepts[<Message>-minimal]: Queue acks a body with only the fields the publisher
  always sends, so it never depends on a field it might not get
- test_accepts[<Message>-extra-fields]: Queue ignores fields it doesn't know
- test_accepts[<Message>-invalid]: Queue dead-letters a body missing a required field

Run them with `make test-contract` (needs a GitLab token for the package registry; see
README.md). In pipelines without the package installed they are skipped.
"""

import pytest

pytest.importorskip("ticketto_contracts", reason="needs the contract group: make test-contract")

# The generated tests and their pytest hook. Wildcard on purpose: the package decides
# which tests exist, from the catalogue.
from ticketto_contracts.suite import *  # noqa: F403  # pylint: disable=wildcard-import,unused-wildcard-import,wrong-import-position

pytestmark = pytest.mark.contract

CONTRACT_SERVICE = "queue-service"
CONTRACT_BODIES = ["extra-fields", "invalid"]
